"""Explicit browser-test server: real media/storage with deterministic model adapters.

Never imported by the production app. Run from repository root:
  python tests/p5_browser_server.py --port 8875
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parents[1]))
for key in ('SILICONFLOW_API_KEY', 'OPENROUTER_API_KEY', 'DASHSCOPE_API_KEY'):
    os.environ[key] = ''

from fastapi.responses import FileResponse
from PIL import Image, ImageDraw, ImageFont
import uvicorn

import vam.config as config
import vam.conversation_service as conversations
from vam.asr.transcriber import MockTranscriber
from vam.config import Settings
from vam.llm.openrouter import OpenRouterClient
from vam.server.app import create_app
from vam.video import index_video_async
from test_p2_transcripts import TranscriptBackend


async def study_model(messages):
    records = json.loads(messages[-1]['content'])
    return {'chapters': [{'title': '图的应用' if i == 0 else '生成树的性质',
        'points': [r['text'], '点击章节或来源时间，可以回到视频核对具体内容。'],
        'source_ids': [r['source_id']]} for i, r in enumerate(records)]}


async def text_answer_model(messages):
    data = json.loads(messages[-1]['content'])
    return json.dumps({'claims': [{'text': '视频讲解了生成树的概念与性质。',
        'evidence_ids': [e['evidence_id']]} for e in data['sources'][:2]], 'warnings': []})


async def fake_chat(self, messages, **kwargs):
    if messages[0]['content'].startswith('Resolve the current question'):
        data = json.loads(messages[-1]['content'])
        return json.dumps({'rewritten_question': '生成树的性质是什么？',
            'anchor_ids': [data['context']['anchors'][0]['evidence_id']], 'needs_clarification': False, 'clarification': ''}), {}
    return '图的应用与生成树。', {}


async def fixture_answer(**kwargs):
    await asyncio.sleep(2)
    if '失败' in kwargs.get('transcript', ''):
        raise RuntimeError('explicit browser test model failure')
    scope = kwargs['retrieval_context']
    await scope.search('blue', top_k=2, sources=['transcript'])
    evidence = await scope.prepare('blue', top_k=2)
    yield {'type': 'final', 'text': '视频介绍了生成树的定义与性质。生成树包含原图全部顶点，并保持连通。',
           'result': {'answer': '生成树的定义与性质。', 'evidence': evidence,
                      'citations': [{'evidence_id': e['evidence_id']} for e in evidence], 'warnings': []}}


async def delayed_indexer(*args, **kwargs):
    await asyncio.sleep(3)
    return await index_video_async(*args, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8875)
    parser.add_argument('--data-root', default='')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='evireel-browser-') as temporary:
        root = Path(temporary)
        picture = Image.new('RGB', (960, 540), '#e6f1ff')
        draw = ImageDraw.Draw(picture)
        font_path = next((p for p in [Path('C:/Windows/Fonts/msyh.ttc'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')] if p.exists()), None)
        font = ImageFont.truetype(str(font_path), 44) if font_path else ImageFont.load_default()
        draw.rounded_rectangle((65, 55, 895, 475), radius=25, fill='#f7faff')
        draw.text((145, 175), 'EviReel | Graph Theory', fill='#16386a', font=font)
        draw.text((145, 265), 'Spanning Trees', fill='#3767b5', font=font)
        picture.save(root/'lecture.png')
        source = root/'lecture.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-loop', '1', '-i', str(root/'lecture.png'),
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000', '-t', '6', '-r', '10', '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', '-y', str(source)], check=True)
        settings = Settings(_env_file=None, data_root=args.data_root or str(root/'assets'),
            frontend_dist=str(Path(__file__).parents[1]/'vam/server/static'),
            enable_retrieval=False, query_include_latest_segment_video=False,
            video_laplacian_min=0, video_diff_threshold=0, video_ssim_threshold=1.1, video_fps=.5,
            frame_store_path=str(root/'default.sqlite3'), study_auto_generate=True)
        config._settings = settings
        OpenRouterClient.chat = fake_chat
        conversations.stream_agent_response = fixture_answer
        transcriber = MockTranscriber([
            {'start': .1, 'end': 2.8, 'text': '生成树是无向连通图的极小连通子图，包含原图所有顶点。'},
            {'start': 3.1, 'end': 5.8, 'text': 'blue 生成树有 n 个顶点和 n-1 条边，任意两点之间存在唯一的简单路径。'},
        ], language='zh')
        app = create_app(settings=settings, backend=TranscriptBackend(), indexer=delayed_indexer, transcriber=transcriber,
                         study_model=study_model, chat_model=text_answer_model)

        @app.post('/__test__/text-mode/{video_id}')
        def text_mode(video_id: str):
            service = app.state.indexing
            service.registry.update_video(video_id, active_generation=None)
            store = service._transcript_store(video_id)
            with store.connect(write=True) as conn:
                conn.execute('UPDATE transcript_heads SET search_version=NULL WHERE video_id=?', (video_id,))
            return {'mode': 'explicit-mock-text-only'}

        @app.get('/__test__/media')
        def media():
            return FileResponse(source, media_type='video/mp4')

        @app.get('/__test__/health')
        def health():
            return {'mode': 'explicit-mock-browser-test'}

        print(f'EXPLICIT TEST SERVER http://127.0.0.1:{args.port}', flush=True)
        uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning', ws_max_size=65536)


if __name__ == '__main__':
    main()
