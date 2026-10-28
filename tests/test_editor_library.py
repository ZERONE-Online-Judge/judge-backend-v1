"""Old Judge-owned imports fail clearly without reaching any transfer API."""
from app.services.store import store
from test_problem_archives import client, context


def test_retired_imports_require_current_role_and_never_mutate(context):
    c=context
    prefix=c['prefix']+'/editor-problems'
    count=len(store.problems)
    for method,path in [('GET',prefix),('POST',prefix+'/'+'a'*32+'/import'),('POST',prefix+'/'+'a'*32+'/packages'),('POST',prefix+'/'+'a'*32+'/updates:preview'),('POST',prefix+'/'+'a'*32+'/updates')]:
        assert client.request(method,path).status_code==401
        assert client.request(method,path,headers=c['reviewer']).status_code==403
        response=client.request(method,path,headers=c['owner'],json={'job_id':'b'*32})
        assert response.status_code==410,response.text
        assert response.json()['error']['code']=='editor_transfer_moved'
        assert c['cid'] in response.json()['error']['details']['editor_url']
    assert len(store.problems)==count
