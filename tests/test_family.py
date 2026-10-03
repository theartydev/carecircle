import copy
import json
import os
import unittest
from unittest.mock import patch, Mock
from datetime import datetime, timezone, timedelta
import app_web
import appointment_worker as worker
from app.storage import dynamodb as ddb

MEMBERS=[{'patient_id':'raj_sharma','name':'Raj Sharma','relationship':'Dad'},
         {'patient_id':'nisha_mehta','name':'Nisha Mehta','relationship':'Mom'}]

class FamilyApiTests(unittest.TestCase):
    def setUp(self):
        p=patch.dict(os.environ,{'CARE_STATE_TABLE':'test'});p.start();self.addCleanup(p.stop)
        p=patch.object(ddb,'list_family_members',return_value=copy.deepcopy(MEMBERS));p.start();self.addCleanup(p.stop)
    def request(self,path,body=None):
        result=app_web.lambda_handler({'rawPath':path,'requestContext':{'http':{'method':'POST' if body is not None else 'GET'}},'body':json.dumps(body or {})},None)
        return result['statusCode'],json.loads(result['body'])
    def test_family_separate_states_and_empty_mom(self):
        with patch.object(ddb,'get_care_state',side_effect=[{'medicines':[{'name':'demo'}]},None]):
            status,body=self.request('/family')
        self.assertEqual(status,200)
        self.assertEqual(body['members'][0]['care_state']['medicines'][0]['name'],'demo')
        self.assertEqual(body['members'][1]['care_state'],{})
    def test_add_member_has_stable_generated_id(self):
        with patch.object(ddb,'save_family_member') as save:
            status,body=self.request('/members',{'name':'Arun Mehta','relationship':'Uncle'})
        self.assertEqual(status,200);self.assertEqual(len(body['member']['patient_id']),32)
        save.assert_called_once_with(body['member'])
    def test_edit_keeps_patient_id_and_care_untouched(self):
        with patch.object(ddb,'save_family_member') as save,patch.object(ddb,'save_care_state') as care:
            status,body=self.request('/members',{'patient_id':'nisha_mehta','name':'Nisha Mehta','relationship':'Mother'})
        self.assertEqual(status,200);self.assertEqual(body['member']['patient_id'],'nisha_mehta');care.assert_not_called()
    def test_duplicate_and_unknown_rejected(self):
        with patch.object(ddb,'save_family_member') as save:
            self.assertEqual(self.request('/members',{'name':'  raj  sharma ','relationship':'Dad'})[0],409)
            self.assertEqual(self.request('/members',{'patient_id':'missing','name':'Someone','relationship':'Other'})[0],404)
        save.assert_not_called()
    def test_wrong_patient_never_writes(self):
        with patch.object(app_web,'_call_bedrock',return_value={'patient':'Raj Sharma'}),patch.object(app_web,'_persist_extracted') as save:
            status,_=self.request('/analyze',{'patient_id':'nisha_mehta','document_text':'test'})
        self.assertEqual(status,409);save.assert_not_called()
    def test_matching_patient_writes_selected_stable_id(self):
        with patch.object(app_web,'_call_bedrock',return_value={'patient':'Nisha Mehta','follow_up':'Review after 7 days.','document_date':'2026-10-03'}),patch.object(app_web,'update_extraction_fields') as save:
            status,body=self.request('/analyze',{'patient_id':'nisha_mehta','document_text':'test'})
        self.assertEqual(status,200);self.assertEqual(save.call_args.args[0],'nisha_mehta')
        self.assertEqual(body['patient_id'],'nisha_mehta')
    def test_family_storage_failure_is_visible(self):
        with patch.object(ddb,'get_care_state',side_effect=RuntimeError('secret')):
            status,body=self.request('/family')
        self.assertEqual(status,500);self.assertNotIn('secret',str(body))

class RegistryTests(unittest.TestCase):
    def test_registry_edit_updates_only_one_nested_member(self):
        client=Mock()
        with patch.object(ddb,'_client',return_value=client),patch.dict(os.environ,{'CARE_STATE_TABLE':'test'}):
            ddb.save_family_member(MEMBERS[1])
        calls=client.update_item.call_args_list
        self.assertEqual(len(calls),2)
        self.assertIn('if_not_exists',calls[0].kwargs['UpdateExpression'])
        self.assertEqual(calls[1].kwargs['ExpressionAttributeNames']['#id'],'nisha_mehta')
        self.assertEqual(calls[1].kwargs['Key']['PK']['S'],'FAMILY#demo')
    def test_saved_registry_survives_read(self):
        client=Mock();client.get_item.return_value={'Item':{'members':ddb._to_ddb({'nisha_mehta':MEMBERS[1]})}}
        with patch.object(ddb,'_client',return_value=client),patch.dict(os.environ,{'CARE_STATE_TABLE':'test'}):
            self.assertEqual(ddb.list_family_members(),[MEMBERS[1]])
        self.assertTrue(client.get_item.call_args.kwargs['ConsistentRead'])

class FamilyWorkerTests(unittest.TestCase):
    def state(self,name):
        day=(datetime.now(timezone.utc).date()+timedelta(days=2)).isoformat()
        return {'patient_name':name,'specialty':'General Medicine','requested_tests':['CBC'],
                'follow_up':{'status':'CONFIRMED','confirmed_date':day}}
    def test_all_members_reminded_and_deduplicated_independently(self):
        seen=set()
        with patch.object(worker,'list_family_members',return_value=MEMBERS), \
             patch.object(worker,'get_care_state',side_effect=lambda pid:self.state(pid)), \
             patch.object(worker,'notification_already_sent',side_effect=lambda pid,key:(pid,key) in seen), \
             patch.object(worker,'record_notification_sent',side_effect=lambda pid,key:seen.add((pid,key))), \
             patch.object(worker,'_send_sns') as send:
            first=worker.lambda_handler({},None);second=worker.lambda_handler({},None)
        self.assertEqual(first['body']['members_checked'],2)
        self.assertTrue(all(r['notification_sent'] for r in first['body']['results']))
        self.assertTrue(all(not r['notification_sent'] for r in second['body']['results']))
        self.assertEqual(send.call_count,2);self.assertEqual(len(seen),2)
        self.assertIn('Raj Sharma',send.call_args_list[0].args[0])
        self.assertIn('Nisha Mehta',send.call_args_list[1].args[0])
    def test_one_member_failure_does_not_block_other(self):
        with patch.object(worker,'list_family_members',return_value=MEMBERS),patch.object(worker,'run_appointment_check',side_effect=[RuntimeError('private'),{'notification_sent':False}]):
            result=worker.lambda_handler({},None)
        self.assertEqual(result['statusCode'],207);self.assertEqual(len(result['body']['results']),2)
        self.assertNotIn('private',str(result))
    def test_empty_mom_does_not_send(self):
        with patch.object(worker,'get_care_state',return_value=None),patch.object(worker,'_send_sns') as send:
            result=worker.run_appointment_check('nisha_mehta',MEMBERS[1])
        self.assertFalse(result['notification_sent']);send.assert_not_called()
