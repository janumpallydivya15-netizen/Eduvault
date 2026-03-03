from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, abort
)
import os
import uuid
from datetime import datetime, timezone
from functools import wraps

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import NoCredentialsError
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

from config import Config

app = Flask(__name__)
app.config.from_object(Config)

# ─────────────────────────────────────────────
# AWS Clients
# ─────────────────────────────────────────────
def get_dynamodb():
    return boto3.resource('dynamodb', region_name=app.config['AWS_REGION'])

def get_s3():
    return boto3.client('s3', region_name=app.config['AWS_REGION'])

def get_sns():
    return boto3.client('sns', region_name=app.config['AWS_REGION'])

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def is_late():
    return datetime.now(timezone.utc) > app.config['ASSIGNMENT_DEADLINE']

def get_user_by_email(email):
    table = get_dynamodb().Table(app.config['DYNAMO_USERS_TABLE'])
    resp = table.scan(FilterExpression=Attr('email').eq(email))
    items = resp.get('Items', [])
    return items[0] if items else None

def get_submissions_by_student(student_id):
    table = get_dynamodb().Table(app.config['DYNAMO_SUBMISSIONS_TABLE'])
    resp = table.scan(FilterExpression=Attr('student_id').eq(student_id))
    return resp.get('Items', [])

def get_all_submissions():
    table = get_dynamodb().Table(app.config['DYNAMO_SUBMISSIONS_TABLE'])
    resp = table.scan()
    return resp.get('Items', [])

def get_submission_by_id(submission_id):
    table = get_dynamodb().Table(app.config['DYNAMO_SUBMISSIONS_TABLE'])
    resp = table.get_item(Key={'submission_id': submission_id})
    return resp.get('Item')

# ─────────────────────────────────────────────
# Decorators
# ─────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please login first.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

def student_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get('role') != 'student':
            abort(403)
        return f(*args, **kwargs)
    return wrapper

def instructor_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get('role') != 'instructor':
            abort(403)
        return f(*args, **kwargs)
    return wrapper

# ─────────────────────────────────────────────
# Public Routes
# ─────────────────────────────────────────────
@app.route('/')
def index():
    if 'user_id' in session:
        if session['role'] == 'student':
            return redirect(url_for('student_dashboard'))
        return redirect(url_for('instructor_dashboard'))
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').lower()
        password = request.form.get('password')

        user = get_user_by_email(email)

        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['user_id']
            session['role'] = user['role']
            session['name'] = user['name']
            session['email'] = user['email']

            flash("Login successful!", "success")

            if user['role'] == 'student':
                return redirect(url_for('student_dashboard'))
            else:
                return redirect(url_for('instructor_dashboard'))

        flash("Invalid email or password", "danger")

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email').lower()
        password = request.form.get('password')
        role = request.form.get('role')

        if get_user_by_email(email):
            flash("Email already exists", "danger")
            return render_template('register.html')

        user_id = str(uuid.uuid4())

        item = {
            'user_id': user_id,
            'name': name,
            'email': email,
            'password': generate_password_hash(password),
            'role': role,
            'created_at': datetime.now(timezone.utc).isoformat()
        }

        get_dynamodb().Table(app.config['DYNAMO_USERS_TABLE']).put_item(Item=item)

        flash("Registration successful! Please login.", "success")
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect(url_for('index'))

# ─────────────────────────────────────────────
# Student Routes
# ─────────────────────────────────────────────
@app.route('/student/dashboard')
@login_required
@student_required
def student_dashboard():
    submissions = get_submissions_by_student(session['user_id'])

    total = len(submissions)
    late = sum(1 for s in submissions if s.get('status') == 'Late')
    graded = sum(1 for s in submissions if s.get('status') == 'Graded')

    marks = [int(s['marks']) for s in submissions if s.get('marks')]
    avg_marks = round(sum(marks)/len(marks), 1) if marks else 0

    return render_template(
        'student_dashboard.html',
        total=total,
        late=late,
        graded=graded,
        avg_marks=avg_marks,
        submissions=submissions
    )

@app.route('/student/upload', methods=['GET', 'POST'])
@login_required
@student_required
def student_upload():

    deadline_passed = datetime.now(timezone.utc) > app.config['ASSIGNMENT_DEADLINE']

    if request.method == 'POST':

        if deadline_passed:
            flash("Deadline has passed. Upload is disabled.", "danger")
            return redirect(url_for('student_upload'))

        assignment_name = request.form.get('assignment_name')
        file = request.files.get('file')

        if not file or not allowed_file(file.filename):
            flash("Invalid file", "danger")
            return redirect(request.url)

        filename = secure_filename(file.filename)
        submission_id = str(uuid.uuid4())
        s3_key = f"{session['user_id']}/{submission_id}/{filename}"

        get_s3().upload_fileobj(file, app.config['S3_BUCKET'], s3_key)

        item = {
            'submission_id': submission_id,
            'student_id': session['user_id'],
            'student_name': session['name'],
            'assignment_name': assignment_name,
            'filename': filename,
            's3_key': s3_key,
            'status': 'Submitted',
            'submitted_at': datetime.now(timezone.utc).isoformat()
        }

        get_dynamodb().Table(app.config['DYNAMO_SUBMISSIONS_TABLE']).put_item(Item=item)

        flash("Assignment uploaded successfully!", "success")
        return redirect(url_for('student_history'))

    return render_template(
        'upload.html',
        deadline_passed=deadline_passed
    )

@app.route('/student/history')
@login_required
@student_required
def student_history():
    submissions = get_submissions_by_student(session['user_id'])
    return render_template('history.html', submissions=submissions)

# ─────────────────────────────────────────────
# Instructor Routes
# ─────────────────────────────────────────────
@app.route('/instructor/dashboard')
@login_required
@instructor_required
def instructor_dashboard():
    submissions = get_all_submissions()

    status_filter = request.args.get('status')
    search_query = request.args.get('search')

    if status_filter:
        submissions = [
            s for s in submissions
            if s.get('status', '').strip().lower() == status_filter.strip().lower()
        ]

    if search_query:
        submissions = [
            s for s in submissions
            if search_query.lower() in s.get('student_name', '').lower()
        ]

    return render_template(
        'instructor_dashboard.html',
        submissions=submissions,
        status_filter=status_filter,
        search_query=search_query
    )

@app.route('/instructor/analytics')
@login_required
@instructor_required
def analytics():
    submissions = get_all_submissions()

    graded = sum(1 for s in submissions if s.get('status') == 'Graded')
    late = sum(1 for s in submissions if s.get('status') == 'Late')
    rejected = sum(1 for s in submissions if s.get('status') == 'Rejected')
    submitted = sum(1 for s in submissions if s.get('status') == 'Submitted')

    chart_data = {
        "labels": ["Graded", "Late", "Rejected", "Submitted"],
        "values": [graded, late, rejected, submitted]
    }

    return render_template(
        "instructor_analytics.html",
        chart_data=chart_data
    )

@app.route('/instructor/grade/<submission_id>', methods=['GET', 'POST'])
@login_required
@instructor_required
def grade_submission(submission_id):
    submission = get_submission_by_id(submission_id)

    if not submission:
        abort(404)

    if request.method == 'POST':
        marks = request.form.get('marks')
        feedback = request.form.get('feedback')

        submission['marks'] = str(marks)
        submission['status'] = 'Graded'
        submission['feedback'] = feedback

        get_dynamodb().Table(
            app.config['DYNAMO_SUBMISSIONS_TABLE']
        ).put_item(Item=submission)

        send_sns_notification(
            "Assignment Graded ✅",
            f"""
Hello {submission.get('student_name')},

Your assignment "{submission.get('assignment_name')}" has been graded.

Marks: {marks}
Feedback: {feedback}

Regards,
EduVault Team
"""
        )

        flash("Assignment graded with feedback!", "success")
        return redirect(url_for('instructor_dashboard'))

    return render_template('grade.html', submission=submission)

@app.route('/instructor/reject/<submission_id>', methods=['POST'])
@login_required
@instructor_required
def reject_submission(submission_id):
    submission = get_submission_by_id(submission_id)

    if not submission:
        abort(404)

    submission['status'] = 'Rejected'

    get_dynamodb().Table(
        app.config['DYNAMO_SUBMISSIONS_TABLE']
    ).put_item(Item=submission)

    # 🔔 SNS Notification
    subject = "Assignment Rejected ❌"
    message = f"""
Hello {submission.get('student_name')},

Your assignment "{submission.get('assignment_name')}" has been rejected.

Please review and resubmit if necessary.

Regards,
EduVault Team
"""

    send_sns_notification(subject, message)

    flash("Submission rejected and student notified!", "danger")
    return redirect(url_for('instructor_dashboard'))

@app.route('/download/<submission_id>')
@login_required
def download_submission(submission_id):
    submission = get_submission_by_id(submission_id)

    if not submission:
        abort(404)

    if session.get('role') == 'student':
        if submission.get('student_id') != session.get('user_id'):
            abort(403)

    try:
        s3 = get_s3()
        url = s3.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': app.config['S3_BUCKET'],
                'Key': submission['s3_key']
            },
            ExpiresIn=300
        )
        return redirect(url)

    except Exception as e:
        flash(f"Download failed: {str(e)}", "danger")
        return redirect(url_for('student_dashboard'))
    
@app.route('/instructor/delete/<submission_id>', methods=['POST'])
@login_required
@instructor_required
def delete_submission(submission_id):
    submission = get_submission_by_id(submission_id)

    if not submission:
        abort(404)

    # Delete from DynamoDB
    get_dynamodb().Table(app.config['DYNAMO_SUBMISSIONS_TABLE']).delete_item(
        Key={'submission_id': submission_id}
    )

    flash("Submission deleted successfully!", "info")
    return redirect(url_for('instructor_dashboard'))
# ─────────────────────────────────────────────
# SNS Notification Helper
# ─────────────────────────────────────────────
def send_sns_notification(subject, message):
    try:
        sns = get_sns()
        sns.publish(
            TopicArn=app.config['SNS_TOPIC_ARN'],
            Subject=subject,
            Message=message
        )
    except Exception as e:
        print("SNS ERROR:", e)

# ─────────────────────────────────────────────
# Error Pages
# ─────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(403)
def forbidden(e):
    return render_template('403.html'), 403

@app.errorhandler(500)
def server_error(e):
    return render_template('500.html'), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)