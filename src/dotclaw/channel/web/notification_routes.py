"""通知历史与浏览器投递 HTTP 路由。"""

from fastapi import APIRouter, Path, Query, Request

from .notification_schemas import (ClaimDeliveryRequest, NotificationChangesResponse, NotificationDeliveryResponse,
    NotificationListResponse, NotificationMemberResponse, NotificationResponse, ReportDeliveryRequest)

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


def _response(notification) -> NotificationResponse:
    return NotificationResponse(
        id=notification.id, kind=notification.kind, occurrence_id=notification.occurrence_id,
        created_at=notification.created_at, read_at=notification.read_at, title=notification.title,
        body=notification.body,
        members=[NotificationMemberResponse(
            task_id=member.task_id, title_snapshot=member.title_snapshot, missed=member.missed,
            overdue=member.overdue, current_status=member.current_status,
            current_is_overdue=member.current_is_overdue,
        ) for member in notification.members],
        deliveries=[NotificationDeliveryResponse(
            notification_id=delivery.notification_id, channel=delivery.channel,
            claim_id=delivery.claim_id, status=delivery.status, attempted_at=delivery.attempted_at,
            reported_at=delivery.reported_at, error=delivery.error,
        ) for delivery in notification.deliveries],
    )


@router.get("", response_model=NotificationListResponse)
async def list_notifications(request: Request, unread_only: bool = False, before_id: int | None = Query(default=None, ge=1), limit: int = Query(default=50, ge=1, le=100)):
    page = await request.app.state.host.notification_service.list(unread_only=unread_only, before_id=before_id, limit=limit)
    return NotificationListResponse(items=[_response(item) for item in page.items], next_before_id=page.next_before_id, unread_count=page.unread_count, watermark=page.watermark)


@router.get("/changes", response_model=NotificationChangesResponse)
async def notification_changes(request: Request, after_id: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=100)):
    page = await request.app.state.host.notification_service.changes(after_id=after_id, limit=limit)
    return NotificationChangesResponse(items=[_response(item) for item in page.items], next_after_id=page.next_after_id, has_more=page.has_more, unread_count=page.unread_count, watermark=page.watermark)


@router.get("/{notification_id}", response_model=NotificationResponse)
async def get_notification(request: Request, notification_id: int = Path(ge=1)):
    return _response(await request.app.state.host.notification_service.get(notification_id))


@router.post("/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_read(request: Request, notification_id: int):
    return _response(await request.app.state.host.notification_service.mark_read(notification_id))


@router.post("/{notification_id}/deliveries/claim")
async def claim_delivery(request: Request, notification_id: int, body: ClaimDeliveryRequest):
    return await request.app.state.host.notification_service.claim_delivery(notification_id, channel=body.channel)


@router.post("/{notification_id}/deliveries/report")
async def report_delivery(request: Request, notification_id: int, body: ReportDeliveryRequest):
    return await request.app.state.host.notification_service.report_delivery(notification_id, claim_id=body.claim_id, status=body.status, error=body.error)
