# CareCircle Architecture

```mermaid
flowchart LR
    U[Family caregiver] -->|HTTPS| W[CareCircle web\nAWS Lambda Function URL]
    W -->|extract documented fields| B[Amazon Bedrock\nAmazon Nova Pro]
    W -->|save care state, follow-up, members| D[(Amazon DynamoDB\nfamily-care-state)]
    W -->|store original synthetic prescription privately| S[(Amazon S3\ncarecircle-documents)]

    E[Amazon EventBridge Scheduler\n8:00 AM Asia/Calcutta] --> A[Appointment worker\nAWS Lambda]
    A -->|read confirmed follow-up\nand requested items| D
    A -->|preparation notification| N[Amazon SNS\nCaregiver email]

    C[Codex + AWS MCP] -.->|read-only AWS resource verification| W
```

## Safety boundary

All demo data is synthetic. CareCircle extracts only documented information,
asks the family to confirm derived follow-up dates, and does not diagnose or
recommend treatment.
