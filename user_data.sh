#!/bin/bash

exec > >(tee /var/log/user-data.log | logger -t user-data) 2>&1

echo "Starting Flask API Deployment"

REPO_URL="https://github.com/your-org/your-repo.git"
REPO_BRANCH="main"
APP_DIR="app"

# Install dependencies
dnf update -y
dnf install -y python3 python3-pip git

# Clone repo
cd /home/ec2-user

git clone -b ${REPO_BRANCH} ${REPO_URL} ${APP_DIR}

cd ${APP_DIR}

python3 -m pip install --upgrade pip --user

# Virtual environment
python3 -m venv venv

source venv/bin/activate

pip install --upgrade pip

pip install -r requirements.txt

# Create systemd service
cat > /etc/systemd/system/flask-api.service <<EOF
[Unit]
Description=Flask PostgreSQL API
After=network.target

[Service]
User=ec2-user
Group=ec2-user

WorkingDirectory=/home/ec2-user/${APP_DIR}

Environment=AWS_REGION=${aws_region}
Environment=DB_SECRET_ARN=${db_secret_arn}
Environment=DB_HOST=${db_host}
Environment=DB_NAME=${db_name}
Environment=DB_PORT=${db_port}
Environment=APP_PORT=${app_port}
Environment=ALLOWED_ORIGINS=${allowed_origins}

ExecStart=/home/ec2-user/${APP_DIR}/venv/bin/gunicorn \
  --workers 4 \
  --bind 0.0.0.0:${app_port} \
  app:app

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable flask-api
systemctl start flask-api

systemctl status flask-api --no-pager

echo "Deployment Complete"