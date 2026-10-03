# CareCircle family workspace release

Implemented: one shared synthetic-demo household, persistent add/edit members, saved per-member care spaces, selected-patient name validation before upload, and scheduled checks across all household members. Existing PATIENT#raj_sharma records and notification keys are preserved. Mom starts as Nisha Mehta with no invented care records. Registry defaults become a saved FAMILY#demo / MEMBERS map upon the first member edit/add. Renaming preserves the member ID and historical care documents.

Permissions: uses existing dynamodb:GetItem and dynamodb:UpdateItem for the registry in family-care-state, plus existing care/notification and S3 permissions. No Scan/Query or new table is required. If existing role policies restrict DynamoDB leading keys to PATIENT# only, explicitly allow FAMILY#demo too. No policy or resource changes are made by the deployment script.

Deploy from the project directory:

    bash deploy-family.sh

This updates BOTH family-care-web and family-care-appointment-worker, preserving all environment variables. It does not invoke the worker or send email. Refresh the browser afterward.

## Short verification

1. Dad opens his existing saved workspace. Switch to Mom: none of Dad's medicines or follow-up should appear.
2. Select Mom, upload demo-mom-nisha.txt, analyze, and confirm the proposed date. Source filename, medicines/tests, and follow-up belong only to Mom.
3. Switch back to Dad. His care state is unchanged. Reload: both workspaces load from DynamoDB.
4. Add a synthetic member and edit their relationship. Reload and verify persistence.
5. Try Dad's prescription while Mom is selected: a patient-name mismatch must appear without writing to S3 or DynamoDB.
6. A worker invocation now returns members_checked and results[], one result per family member. A member failure returns an aggregate 207 while other members are still checked. Invoking may send real SNS emails for each eligible member. Repeat to verify per-member duplicate prevention.

Limitations stated in UI: shared public synthetic demo (no user accounts); one latest prescription and current follow-up per member; .txt uploads only; report readiness remains demo data, not actual report matching. Add/edit is supported; destructive member deletion is intentionally absent. Reminder delivery uses the existing SNS caregiver topic for the household. Do not upload real patient data.
