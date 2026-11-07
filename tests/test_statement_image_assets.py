import hashlib
import io
import json
import zipfile
from datetime import timedelta

from app.models import now_utc
from app.services.storage import object_storage
from app.services.store import store
from test_problem_archives import context, client, export, mutate_zip, upload


def test_inline_images_and_editorial_privacy_survive_archive_copy_and_reexport(context):
    c = context
    public = next(asset for asset in store.problem_assets_for_problem(c['cid'], c['pid']) if asset.mime_type == 'image/png')
    private_key = f"contests/{c['cid']}/problems/{c['pid']}/editorial-assets/answer.png"
    content = b'\x89PNG\r\n\x1a\nprivate-answer-image'
    object_storage.write_bytes(private_key, content, 'image/png')
    private = store.create_problem_asset(c['cid'], c['pid'], 'answer.png', private_key,
        'image/png', len(content), hashlib.sha256(content).hexdigest())
    attrs = ' alt="설명 &amp; 그림" data-zoj-align="right" data-zoj-width="75%"'
    store.update_problem(c['cid'], c['pid'], statement=f'<img src="asset://{public.asset_id}"{attrs}>',
        editorial=f'<img src="asset://{private.asset_id}"{attrs}>',
        statement_translations={'en': {'title': 'Image', 'statement': f'<img src="asset://{public.asset_id}"{attrs}>'}},
        editorial_translations={'en': f'<img src="asset://{private.asset_id}"{attrs}>'})
    archive = export(c)
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        manifest = json.loads(source.read('manifest.json'))
        assert next(item for item in manifest['assets'] if item['original_filename'] == 'answer.png')['category'] == 'editorial-assets'
        assert attrs in manifest['problem']['statement']
    imported = upload(c, archive, 'import')
    assert imported.status_code == 200, imported.text
    target = imported.json()['data']
    again = upload(c, export({**c, 'pid': target['problem_id']}), 'import', code='C')
    assert again.status_code == 200, again.text
    copied = client.post(c['prefix'] + '/problems:copy', headers=c['owner'], json={
        'source_problem_id': target['problem_id'], 'target_division_id': c['division'], 'problem_code': 'D'})
    assert copied.status_code == 200, copied.text
    targets = [target, again.json()['data'], copied.json()['data']]
    store.update_contest_settings(c['cid'], status='ended', start_at=now_utc()-timedelta(days=1),
        end_at=now_utc()-timedelta(hours=1), problem_access_after_end='public', editorial_access_after_end='private')
    for problem in targets:
        pid = problem['problem_id']
        all_assets = store.problem_assets_for_problem(c['cid'], pid)
        answer = next(asset for asset in all_assets if asset.original_filename == 'answer.png')
        assert '/editorial-assets/' in answer.storage_key
        assert f'src="asset://{answer.asset_id}"' in problem['editorial']
        assert attrs in problem['statement_translations']['en']['statement']
        result = client.get(f"/api/contests/{c['cid']}/problems/{pid}/assets")
        assert result.status_code == 200, result.text
        visible = result.json()['data']
        assert [asset['original_filename'] for asset in visible] == ['그림.png']
        assert answer.asset_id not in result.text and answer.storage_key not in result.text
        assert client.get(visible[0]['download_url']).status_code == 200
    store.update_contest_settings(c['cid'], editorial_access_after_end='public')
    result = client.get(f"/api/contests/{c['cid']}/problems/{target['problem_id']}/assets")
    assert {asset['original_filename'] for asset in result.json()['data']} == {'그림.png', 'answer.png'}


def test_new_archive_category_does_not_allow_unknown_resources_or_missing_html_assets(context):
    c = context
    archive = export(c)
    unknown = mutate_zip(archive, lambda manifest, files: manifest['assets'][0].update(category='arbitrary-private'))
    assert upload(c, unknown).status_code == 422
    missing = mutate_zip(archive, lambda manifest, files: manifest['problem'].update(
        statement='<img src="asset://asset-9999" data-zoj-align="center">'))
    assert upload(c, missing).status_code == 422
