/** WHEP (RFC 9725) playback with the browser's RTCPeerConnection. The browser
 * plays the camera's WHEP URL directly from the media server.
 *
 * Returns a stop function that closes the connection and DELETEs the session
 * URL if the server provided one. */
export async function playWhep(url: string, video: HTMLVideoElement, signal?: AbortSignal): Promise<() => void> {
  const parsed = new URL(url);
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    throw new Error(`WHEP URL must be http(s), got ${parsed.protocol}`);
  }
  const pc = new RTCPeerConnection();
  let session: string | null = null;
  const stop = () => {
    pc.close();
    if (session) fetch(session, { method: "DELETE" }).catch(() => {});
    session = null;
  };
  try {
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });
    pc.ontrack = (e) => {
      if (e.track.kind === "video") video.srcObject = e.streams[0] ?? new MediaStream([e.track]);
    };
    await pc.setLocalDescription(await pc.createOffer());
    await iceGatheringDone(pc, 2000);
    const res = await fetch(parsed.toString(), {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: pc.localDescription!.sdp,
      signal,
    });
    if (res.status !== 201 && res.status !== 200) throw new Error(`WHEP server answered HTTP ${res.status}`);
    const location = res.headers.get("Location");
    if (location) session = new URL(location, parsed).toString();
    await pc.setRemoteDescription({ type: "answer", sdp: await res.text() });
    return stop;
  } catch (err) {
    stop();
    throw err;
  }
}

/** No trickle ICE, so the offer has to carry the candidates; after `ms` we
 * send whatever has been gathered. */
function iceGatheringDone(pc: RTCPeerConnection, ms: number): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener("icegatheringstatechange", check);
      clearTimeout(timer);
      resolve();
    };
    const check = () => { if (pc.iceGatheringState === "complete") done(); };
    const timer = setTimeout(done, ms);
    pc.addEventListener("icegatheringstatechange", check);
  });
}
