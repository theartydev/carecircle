import json
import os
import unittest
from unittest.mock import patch
from botocore.exceptions import ClientError
import app_web
from app.storage import dynamodb

class FollowupEndpointTests(unittest.TestCase):
    def request(self):
        return app_web.lambda_handler({
            'rawPath': '/followup', 'requestContext': {'http': {'method': 'POST'}},
            'body': json.dumps({'patient_id': 'raj_sharma', 'action': 'confirm',
                                'confirmed_date': '2026-10-05'})}, None)

    def test_confirmation_persists_before_success(self):
        fu = {'status': 'AWAITING_CONFIRMATION', 'suggested_date': '2026-10-05',
              'source_instruction': 'Review after 7 days.'}
        with patch.dict(os.environ, {'CARE_STATE_TABLE': 'family-care-state'}), \
             patch.object(dynamodb, 'get_care_state', return_value={'follow_up': fu}) as read, \
             patch.object(app_web, 'update_followup_state') as write:
            response = self.request()
        self.assertEqual(response['statusCode'], 200)
        result = json.loads(response['body'])['follow_up']
        self.assertEqual(result['status'], 'CONFIRMED')
        self.assertEqual(result['confirmed_date'], '2026-10-05')
        self.assertFalse(result['requires_human_confirmation'])
        read.assert_called_once_with('raj_sharma')
        write.assert_called_once_with('raj_sharma', result)

    def test_storage_failures_are_logged_and_redacted(self):
        for stage in ('read', 'write'):
            with self.subTest(stage=stage), \
                 patch.dict(os.environ, {'CARE_STATE_TABLE': 'family-care-state'}), \
                 patch.object(dynamodb, 'get_care_state', return_value={}) as read, \
                 patch.object(app_web, 'update_followup_state') as write:
                target = read if stage == 'read' else write
                target.side_effect = ClientError({'Error': {'Code': 'AccessDeniedException',
                    'Message': 'PRIVATE CONTENT'}}, 'GetItem' if stage == 'read' else 'UpdateItem')
                with self.assertLogs(app_web.logger, level='INFO') as logs:
                    response = self.request()
                self.assertEqual(response['statusCode'], 500)
                output = '\n'.join(logs.output)
                self.assertIn('stage=' + stage, output)
                self.assertIn('AccessDeniedException', output)
                self.assertNotIn('PRIVATE CONTENT', output + response['body'])
                self.assertNotIn('persistence succeeded', output)
                if stage == 'read':
                    write.assert_not_called()
