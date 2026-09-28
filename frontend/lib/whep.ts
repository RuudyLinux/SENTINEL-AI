/** WHEP (WebRTC-HTTP Egress Protocol, RFC 9725) playback with the browser's own
 * RTCPeerConnection — no library. The official camera catalogue gives each
 * camera a WHEP URL next to its RTSP one; the browser plays it directly from
 * the camera's media server, sub-second, and the backend spends nothing on it.
 *
 * Returns a stop function that closes the peer connection and, when the server
 * gave a session URL, ends the session with DELETE as the protocol asks. */
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

/** The offer is sent once, not trickled, so it must carry the candidates. A
 * browser that has not finished gathering in `ms` sends what it has. */
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
