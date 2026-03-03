from datetime import datetime, timezone

class Config:
    SECRET_KEY = "eduvault-super-secure-key"

    AWS_REGION = "ap-south-1"

    DYNAMO_USERS_TABLE = "EduVault_Users"
    DYNAMO_SUBMISSIONS_TABLE = "EduVault_Submissions"

    S3_BUCKET = "eduvault-submissions-mumbai"

    SNS_TOPIC_ARN = "arn:aws:sns:ap-south-1:120121146931:EduVault-Notifications"

    ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "ppt", "pptx", "zip"}

    ASSIGNMENT_DEADLINE = datetime(2026, 3, 5, 23, 59, tzinfo=timezone.utc)

    LATE_PENALTY = 10