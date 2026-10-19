import pytest
from pydantic import ValidationError
from app.language_limits import effective_limits, agent_problem
from app.models import ProblemLanguageResourceLimit
from app.routers.operator import ProblemLanguageResourceLimitPayload, _clean_language_resource_limits
from app.services.problem_archive import ResourceLimit
from app.services.editor_judging import Snapshot


@pytest.mark.parametrize("language,time,memory", [("cpp17",1000,256),("c99",1000,256),("python313",5000,544),("pypy3",5000,640),("java8",3000,528),("kotlin",3000,528)])
def test_effective_default_and_disable(language,time,memory):
    assert effective_limits(1000,256,language)=={"time_limit_ms":time,"memory_limit_mb":memory}
    disabled={"disable_auto_adjustment":True}
    assert effective_limits(1000,256,language,disabled)=={"time_limit_ms":1000,"memory_limit_mb":256}
    assert effective_limits(2000,512,language,disabled)=={"time_limit_ms":2000,"memory_limit_mb":512}
    assert effective_limits(1000,256,language,{**disabled,"time_limit_ms":750})=={"time_limit_ms":750,"memory_limit_mb":256}


def test_assignment_resolves_disabled_defaults_for_existing_agents_without_mutation():
    original={"time_limit_ms":1000,"memory_limit_mb":256,"language_resource_limits":{"pypy3":{"disable_auto_adjustment":True,"memory_limit_mb":384},"kotlin":{"time_limit_ms":700}}}
    result=agent_problem(original)
    assert result["language_resource_limits"]=={"pypy3":{"time_limit_ms":1000,"memory_limit_mb":384},"kotlin":{"time_limit_ms":700}}
    assert original["language_resource_limits"]["pypy3"]["disable_auto_adjustment"] is True


def test_disable_flag_survives_operator_storage_and_archive_models():
    value={"time_limit_ms":750,"disable_auto_adjustment":True}
    for model in (ProblemLanguageResourceLimit,ProblemLanguageResourceLimitPayload,ResourceLimit):
        assert model(**value).model_dump(exclude_none=True)==value
    assert _clean_language_resource_limits({"pypy3":ProblemLanguageResourceLimitPayload(**value)})=={"pypy3":value}
    assert _clean_language_resource_limits({"pypy3":ProblemLanguageResourceLimitPayload()})=={}
    with pytest.raises(ValidationError): ProblemLanguageResourceLimitPayload(disable_auto_adjustment="false")


def test_editor_snapshot_retains_limits_and_rejects_unknown_languages():
    value={"format":"zoj-editor-judge-v1","operation_id":"a"*32,"project_id":"b"*32,"context_hash":"c"*64,
           "source":{"name":"a.py","language":"pypy3","sha256":"d"*64},"time_limit_ms":1000,"memory_limit_mb":256,
           "testcases":[{"index":1,"input_sha":"e"*64,"answer_sha":"f"*64}],"resources":[{"name":"check.cpp","role":"checker","sha256":"a"*64}],
           "language_resource_limits":{"pypy3":{"disable_auto_adjustment":True,"time_limit_ms":700}}}
    parsed=Snapshot.model_validate(value)
    assert parsed.language_resource_limits["pypy3"].disable_auto_adjustment
    value["language_resource_limits"]={"cpp20":{"time_limit_ms":700}}
    with pytest.raises(ValidationError): Snapshot.model_validate(value)


from test_problem_archives import client, context, export, upload
from app.services.store import store


def test_disabled_policy_survives_problem_copy_package_import_and_patch(context):
    c=context
    limits={"pypy3":{"disable_auto_adjustment":True,"time_limit_ms":700},"python313":{"memory_limit_mb":384}}
    result=client.patch(c["prefix"]+"/problems/"+c["pid"],headers=c["owner"],json={"language_resource_limits":limits})
    assert result.status_code==200,result.text
    assert result.json()["data"]["language_resource_limits"]["pypy3"]["disable_auto_adjustment"] is True
    copied=client.post(c["prefix"]+"/problems:copy",headers=c["owner"],json={"source_problem_id":c["pid"],"target_division_id":c["division"],"problem_code":"C"})
    assert copied.status_code==200,copied.text
    assert copied.json()["data"]["language_resource_limits"]["pypy3"]["disable_auto_adjustment"] is True
    imported=upload(c,export(c),"import")
    assert imported.status_code==200,imported.text
    restored=store.get_problem(imported.json()["data"]["problem_id"])
    assert restored.language_resource_limits["pypy3"].disable_auto_adjustment
    assert restored.language_resource_limits["python313"].memory_limit_mb==384

from test_editor_judging import database, snapshot, submit, node, claim, SECRET
from app.judge_languages import ICPC_CAPABILITY
from app.orm_models import EditorJudgeRunRow
import hashlib


def test_editor_http_snapshot_to_agent_assignment_preserves_policy_and_separates_datasets(database):
    def change(disabled):
        def apply(manifest,files):
            source=b'print(sum(map(int,input().split())))\n'
            sha=hashlib.sha256(source).hexdigest()
            files['blobs/'+sha]=source
            manifest.update(project_id='b'*32,language_resource_limits={'pypy3':{'disable_auto_adjustment':disabled,'memory_limit_mb':384}})
            manifest['source'].update(name='main.py',language='pypy3',sha256=sha)
        return apply
    node_id=node()
    store.register_node('editor-test-node',SECRET,2,'0.2.27',execution_capabilities=[ICPC_CAPABILITY])
    first=submit(snapshot(change=change(True)))
    second=submit(snapshot(change=change(False)))
    assert first.status_code==202,first.text
    assert second.status_code==202,second.text
    jobs=claim(node_id,2)
    assert len(jobs)==2
    limits={job['judge_job_id']:job['problem']['language_resource_limits']['pypy3'] for job in jobs}
    assert limits[first.json()['data']['id']]=={'time_limit_ms':1000,'memory_limit_mb':384}
    assert limits[second.json()['data']['id']]=={'memory_limit_mb':384}
    with database() as db:
        a=db.get(EditorJudgeRunRow,first.json()['data']['id'])
        b=db.get(EditorJudgeRunRow,second.json()['data']['id'])
        assert a.dataset_key!=b.dataset_key
        assert a.payload['language_resource_limits']['pypy3']['disable_auto_adjustment'] is True
