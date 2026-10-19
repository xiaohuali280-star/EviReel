from fastapi import APIRouter, Query, Request
from pydantic import Field

from vam.protocol import StrictSchemaModel

router = APIRouter(prefix='/api/conversations', tags=['Conversations'])


class CreateConversation(StrictSchemaModel):
    video_id: str = Field(pattern=r'^v_[0-9a-f]{32}$')


@router.post('', status_code=201)
async def create(body: CreateConversation, request: Request):
    request.app.state.indexing._usable_video(body.video_id)
    return request.app.state.conversations.store.create(body.video_id)


@router.get('')
async def list_conversations(request: Request, video_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    return request.app.state.conversations.store.list(video_id, offset, limit)


@router.get('/{conversation_id}')
async def detail(conversation_id: str, request: Request):
    return request.app.state.conversations.store.get(conversation_id)


@router.get('/{conversation_id}/messages')
async def messages(conversation_id: str, request: Request, after: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
    return request.app.state.conversations.store.messages(conversation_id, after, limit)


@router.get('/{conversation_id}/requests/{request_id}')
async def request_state(conversation_id: str, request_id: str, request: Request):
    service = request.app.state.conversations
    row = service.store.request(conversation_id, request_id)
    if row['result']:
        row['result'] = service.store.mark_snapshot(row['result'], service.store.get(conversation_id)['video_id'])
    return row


@router.delete('/{conversation_id}', status_code=204)
async def delete(conversation_id: str, request: Request):
    request.app.state.conversations.store.delete(conversation_id)
