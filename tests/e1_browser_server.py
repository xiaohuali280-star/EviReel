"""Explicit E1 browser-test server: platform mode on REAL PostgreSQL + REAL S3-compatible storage.

Model calls use the deterministic P1 colour backend (no paid API). Never imported by the app.
  EVIREEL_TEST_DATABASE_URL, EVIREEL_TEST_S3_ENDPOINT/ACCESS_KEY/SECRET_KEY  (as in tests/e1_support.py)
  EVIREEL_E2E_PASSWORD   password for the generated users alice/bob (set by the runner, never logged)
  python tests/e1_browser_server.py --port 8876
"""
import argparse
import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1]))
for key in ('SILICONFLOW_API_KEY', 'OPENROUTER_API_KEY', 'DASHSCOPE_API_KEY'):
    os.environ[key] = ''

import uvicorn

import vam.config as config
from e1_support import MiB, S3, make_video, scratch_bucket, scratch_database
from test_p1_video_management import ColorBackend
from vam.config import Settings
from vam.platform.db import PlatformDB
from vam.platform.identity import IdentityService
from vam.server.app import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8876)
    parser.add_argument('--url-ttl', type=int, default=3)
    args = parser.parse_args()
    password = os.environ['EVIREEL_E2E_PASSWORD']
    with tempfile.TemporaryDirectory(prefix='evireel-e1-browser-') as temporary, \
            scratch_database() as db_url, scratch_bucket() as (bucket, _):
        root = Path(temporary)
        fixture = make_video(root / 'lecture.mp4', 21 * MiB)
        other = make_video(root / 'other.mp4', 21 * MiB, seconds=2)
        PlatformDB(db_url).migrate()
        settings = Settings(_env_file=None, data_root=str(root / 'assets'), frame_store_path=str(root / 'default.sqlite3'),
                            frontend_dist=str(Path(__file__).parents[1] / 'vam/server/static'),
                            enable_retrieval=False, asr_enabled=False, video_fps=1, video_laplacian_min=0,
                            video_diff_threshold=0, video_ssim_threshold=1.1, enable_event_finalize=False,
                            query_include_latest_segment_video=False, study_auto_generate=False,
                            database_url=db_url, s3_endpoint=S3['endpoint'], s3_public_endpoint=S3['endpoint'],
                            s3_access_key=S3['access_key'], s3_secret_key=S3['secret_key'], s3_bucket=bucket,
                            upload_part_size=5 * MiB, upload_url_ttl_s=args.url_ttl, upload_reconcile_interval_s=5)
        config._settings = settings
        app = create_app(settings=settings, backend=ColorBackend())
        identity = IdentityService(PlatformDB(db_url), settings)
        for name in ('alice', 'bob'):
            identity.create_user(name, password)

        @app.get('/__test__/health')
        def health():
            return {'mode': 'explicit-e1-browser-test', 'storage': 'real-s3', 'database': 'real-postgresql'}

        @app.get('/__test__/fixtures')
        def fixtures():
            return {name: {'path': str(path), 'size': path.stat().st_size,
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                    for name, path in (('lecture', fixture), ('other', other))}

        @app.get('/__test__/object-sha256/{upload_id}')
        def object_sha(upload_id: str):
            with app.state.platform.db.connect() as conn:
                row = conn.execute('SELECT object_key FROM upload_sessions WHERE upload_id=%s', (upload_id,)).fetchone()
            digest = hashlib.sha256()
            for chunk in app.state.platform.store.iter_object(row['object_key']):
                digest.update(chunk)
            return {'sha256': digest.hexdigest()}

        print(f'EXPLICIT E1 TEST SERVER http://127.0.0.1:{args.port}', flush=True)
        uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning', ws_max_size=65536)


if __name__ == '__main__':
    main()
