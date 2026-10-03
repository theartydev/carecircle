"""Offline handler-to-DynamoDB regression tests; no AWS requests."""
import json
import unittest
from unittest.mock import patch, Mock
from botocore.exceptions import ClientError
import app_web
from app.storage import dynamodb


class AnalyzeEndpointTests(unittest.TestCase):
    def setUp(self):
        self.extracted = {
            'patient': 'Raj Sharma', 'document_date': '28 September 2026',
            'follow_up': 'Review after 7 days.', 'medicines': [],
            'requested_tests': [],
        }
        self.client = Mock()
        self.env = patch.dict('os.environ', {'CARE_STATE_TABLE': 'family-care-state'})
        self.env.start()
        self.addCleanup(self.env.stop)
        for patcher in (patch.object(app_web, '_call_bedrock', return_value=self.extracted),
                        patch.object(dynamodb, '_client', return_value=self.client)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def request(self):
        response = app_web.lambda_handler({
            'requestContext': {'http': {'method': 'POST'}}, 'rawPath': '/analyze',
            'body': json.dumps({'document_text': 'synthetic prescription'}),
        }, None)
        return response['statusCode'], json.loads(response['body'])

    def test_handler_writes_structured_followup_and_timestamp(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertTrue(body['persisted'])
        self.client.update_item.assert_called_once()
        call = self.client.update_item.call_args.kwargs
        self.assertEqual(call['TableName'], 'family-care-state')
        self.assertEqual(call['Key'], {'PK': {'S': 'PATIENT#raj_sharma'}, 'SK': {'S': 'CARE_STATE'}})
        values = call['ExpressionAttributeValues']
        followup = dynamodb._from_ddb(values[':follow_up'])
        self.assertEqual(followup, body['follow_up_state'])
        self.assertEqual(followup['suggested_date'], '2026-10-05')
        self.assertIn(':updated_at', values)
        self.assertNotIn('appointment', call['ExpressionAttributeNames'].values())
        self.assertNotIn('document_availability', call['ExpressionAttributeNames'].values())
        self.client.put_item.assert_not_called()

    def test_write_failure_is_not_success_and_logs_are_redacted(self):
        for code in ('AccessDeniedException', 'ValidationException', 'ResourceNotFoundException'):
            with self.subTest(code=code):
                self.client.update_item.side_effect = ClientError(
                    {'Error': {'Code': code, 'Message': 'PRIVATE MEDICAL CONTENT'}}, 'UpdateItem')
                with self.assertLogs(app_web.logger, level='INFO') as logs:
                    status, body = self.request()
                self.assertEqual(status, 500)
                self.assertFalse(body['persisted'])
                output = '\n'.join(logs.output)
                self.assertIn(code, output)
                self.assertNotIn('Persistence succeeded', output)
                for private in ('PRIVATE MEDICAL CONTENT', 'Raj Sharma', 'Review after 7 days.'):
                    self.assertNotIn(private, output + json.dumps(body))

    def test_missing_table_returns_503_without_write(self):
        with patch.dict('os.environ', {'CARE_STATE_TABLE': ' '}):
            status, body = self.request()
        self.assertEqual(status, 503)
        self.assertFalse(body['persisted'])
        self.client.update_item.assert_not_called()
        app_web._call_bedrock.assert_not_called()

    def test_missing_patient_returns_422_without_write(self):
        self.extracted['patient'] = None
        status, body = self.request()
        self.assertEqual(status, 422)
        self.assertFalse(body['persisted'])
        self.client.update_item.assert_not_called()

    def test_patient_whitespace_uses_existing_key(self):
        self.extracted['patient'] = '  Raj  Sharma  '
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body['patient_id'], 'raj_sharma')


if __name__ == '__main__':
    unittest.main()
