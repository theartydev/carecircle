import base64
import json
import os
import unittest
from unittest.mock import patch, Mock
import app_web
from app.storage import dynamodb

class UploadTests(unittest.TestCase):
    def setUp(self):
        self.s3=Mock(); self.ddb=Mock()
        self.extracted={'patient':'Raj Sharma','document_date':'28 September 2026','follow_up':'Review after 7 days.'}
        for p in [patch.dict(os.environ, {'CARE_STATE_TABLE':'family-care-state','CARE_DOCUMENT_BUCKET':'demo-bucket'}),
                  patch.object(app_web,'_call_bedrock',return_value=self.extracted),
                  patch.object(app_web.boto3,'client',return_value=self.s3),
                  patch.object(dynamodb,'_client',return_value=self.ddb)]:
            p.start();self.addCleanup(p.stop)
        self.raw=b'Patient: Raj Sharma\nFollow-up: Review after 7 days.\n'
        self.body={'file_name':'cardiology-28-sep.txt','file_base64':base64.b64encode(self.raw).decode()}
    def request(self):
        return app_web.lambda_handler({'rawPath':'/analyze','requestContext':{'http':{'method':'POST'}},'body':json.dumps(self.body)},None)
    def test_original_stored_and_linked_to_care_state(self):
        response=self.request(); self.assertEqual(response['statusCode'],200)
        stored=self.s3.put_object.call_args.kwargs
        self.assertEqual(stored['Body'],self.raw)
        self.assertEqual(stored['ServerSideEncryption'],'AES256')
        self.assertNotIn('ACL',stored)
        values=self.ddb.update_item.call_args.kwargs['ExpressionAttributeValues']
        self.assertEqual(values[':source_document']['S'],'cardiology-28-sep.txt')
        ref=dynamodb._from_ddb(values[':source_storage'])
        self.assertEqual(ref['key'],stored['Key'])
        self.assertEqual(ref['bucket'],'demo-bucket')
        self.assertEqual(dynamodb._from_ddb(values[':follow_up'])['suggested_date'],'2026-10-05')
    def test_bad_files_rejected_before_model_or_storage(self):
        for changes in [{'file_name':'x.pdf'},{'file_name':'../x.txt'}, {'file_base64':'!'},
                        {'file_base64':base64.b64encode(b'\xff').decode()}, {'file_base64':'A'*136537}]:
            with self.subTest(changes=changes):
                original=self.body.copy();self.body.update(changes)
                self.assertEqual(self.request()['statusCode'],400); self.body=original
        app_web._call_bedrock.assert_not_called();self.s3.put_object.assert_not_called()
    def test_missing_bucket(self):
        with patch.dict(os.environ,{'CARE_DOCUMENT_BUCKET':''}):
            self.assertEqual(self.request()['statusCode'],503)
        app_web._call_bedrock.assert_not_called()
    def test_s3_failure_does_not_claim_save(self):
        self.s3.put_object.side_effect=RuntimeError('private')
        response=self.request()
        self.assertEqual(response['statusCode'],500)
        self.assertNotIn('private',response['body']);self.ddb.update_item.assert_not_called()
    def test_database_failure_after_upload_is_reported(self):
        self.ddb.update_item.side_effect=RuntimeError('failed')
        response=self.request()
        self.assertEqual(response['statusCode'],500)
        self.assertFalse(json.loads(response['body'])['persisted'])
