#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
zip -qr carecircle-family-web.zip app_web.py app -x '*/__pycache__/*' '*.pyc'
zip -qr carecircle-family-worker.zip appointment_worker.py app -x '*/__pycache__/*' '*.pyc'
aws lambda update-function-code --function-name family-care-web --zip-file fileb://carecircle-family-web.zip --profile family-care-agent --region us-east-1 --no-cli-pager --query '{Function:FunctionName,Status:LastUpdateStatus}'
aws lambda wait function-updated --function-name family-care-web --profile family-care-agent --region us-east-1
aws lambda update-function-code --function-name family-care-appointment-worker --zip-file fileb://carecircle-family-worker.zip --profile family-care-agent --region us-east-1 --no-cli-pager --query '{Function:FunctionName,Status:LastUpdateStatus}'
aws lambda wait function-updated --function-name family-care-appointment-worker --profile family-care-agent --region us-east-1
printf 'Both Lambda updates completed. Refresh CareCircle to load the family dashboard.\n'
