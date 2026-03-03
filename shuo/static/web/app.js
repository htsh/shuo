const toggleBtn = document.getElementById("toggle");
const muteBtn = document.getElementById("mute");
const logEl = document.getElementById("log");
const transcriptEl = document.getElementById("transcript");
const connectionChip = document.getElementById("status-connection");
const turnChip = document.getElementById("status-turn");
const micChip = document.getElementById("status-mic");

let ws = null;
let audioCtx = null;
let outputGain = null;
let mediaStream = null;
let mediaSource = null;
let captureNode = null;

let running = false;
let muted = false;
let playbackCursor = 0;
let activeSources = new Set();
let pendingPcm = [];

function logLine(message) {
  const ts = new Date().toLocaleTimeString();
  logEl.textContent += `[${ts}] ${message}\n`;
  logEl.scrollTop = logEl.scrollHeight;
}

function setConnection(status) {
  connectionChip.textContent = status;
}

function setTurn(status) {
  turnChip.textContent = status;
}

function setMic(status) {
  micChip.textContent = status;
}

function clearPlayback() {
  for (const source of activeSources) {
    try {
      source.stop();
    } catch {
      // no-op
    }
  }
  activeSources.clear();
  if (audioCtx) {
    playbackCursor = audioCtx.currentTime;
  }
}

function decodeMulawByte(byteVal) {
  let x = (~byteVal) & 0xff;
  const sign = x & 0x80;
  const exponent = (x >> 4) & 0x07;
  const mantissa = x & 0x0f;
  let sample = ((mantissa << 3) + 0x84) << exponent;
  sample = sign ? 0x84 - sample : sample - 0x84;
  if (sample > 32767) return 32767;
  if (sample < -32768) return -32768;
  return sample;
}

function mulawBase64ToFloat32(audioB64) {
  const binary = atob(audioB64);
  const len = binary.length;
  const float = new Float32Array(len);

  for (let i = 0; i < len; i += 1) {
    const pcm = decodeMulawByte(binary.charCodeAt(i));
    float[i] = pcm / 32768;
  }

  return float;
}

function schedulePlayback(audioB64) {
  if (!audioCtx || !outputGain || muted) return;

  const floatChunk = mulawBase64ToFloat32(audioB64);
  const buffer = audioCtx.createBuffer(1, floatChunk.length, 8000);
  buffer.copyToChannel(floatChunk, 0);

  const source = audioCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(outputGain);

  const startAt = Math.max(audioCtx.currentTime, playbackCursor);
  source.start(startAt);
  playbackCursor = startAt + buffer.duration;

  activeSources.add(source);
  source.onended = () => activeSources.delete(source);
}

function resampleTo16k(input, inputRate) {
  if (inputRate === 16000) return input;

  const ratio = inputRate / 16000;
  const outLength = Math.max(1, Math.floor(input.length / ratio));
  const out = new Float32Array(outLength);

  for (let i = 0; i < outLength; i += 1) {
    const srcPos = i * ratio;
    const idx = Math.floor(srcPos);
    const frac = srcPos - idx;
    const a = input[idx] || 0;
    const b = input[idx + 1] ?? a;
    out[i] = a + (b - a) * frac;
  }

  return out;
}

function pushPcmChunk(floatChunk) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;

  const pcm16 = resampleTo16k(floatChunk, audioCtx.sampleRate);
  for (let i = 0; i < pcm16.length; i += 1) {
    const s = Math.max(-1, Math.min(1, pcm16[i]));
    pendingPcm.push(s < 0 ? Math.round(s * 32768) : Math.round(s * 32767));
  }

  while (pendingPcm.length >= 320) {
    const frame = pendingPcm.splice(0, 320);
    const buffer = new ArrayBuffer(frame.length * 2);
    const view = new DataView(buffer);

    for (let i = 0; i < frame.length; i += 1) {
      view.setInt16(i * 2, frame[i], true);
    }

    ws.send(buffer);
  }
}

async function attachMicrophone() {
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
    video: false,
  });

  mediaSource = audioCtx.createMediaStreamSource(mediaStream);

  if (audioCtx.audioWorklet) {
    await audioCtx.audioWorklet.addModule("/static/web/mic-worklet.js");
    const worklet = new AudioWorkletNode(audioCtx, "pcm-capture-processor");
    worklet.port.onmessage = (evt) => {
      if (running && evt.data) pushPcmChunk(evt.data);
    };
    mediaSource.connect(worklet);
    captureNode = worklet;
    return;
  }

  const scriptNode = audioCtx.createScriptProcessor(2048, 1, 1);
  scriptNode.onaudioprocess = (evt) => {
    if (!running) return;
    const channel = evt.inputBuffer.getChannelData(0);
    pushPcmChunk(new Float32Array(channel));
  };
  mediaSource.connect(scriptNode);
  scriptNode.connect(audioCtx.destination);
  captureNode = scriptNode;
}

async function cleanupAudio() {
  clearPlayback();
  pendingPcm = [];

  if (captureNode) {
    try {
      captureNode.disconnect();
    } catch {
      // no-op
    }
    captureNode = null;
  }

  if (mediaSource) {
    try {
      mediaSource.disconnect();
    } catch {
      // no-op
    }
    mediaSource = null;
  }

  if (mediaStream) {
    mediaStream.getTracks().forEach((track) => track.stop());
    mediaStream = null;
  }

  if (audioCtx) {
    try {
      await audioCtx.close();
    } catch {
      // no-op
    }
    audioCtx = null;
    outputGain = null;
  }
}

function handleServerMessage(payload) {
  if (payload.type === "session.ready") {
    setConnection("connected");
    setTurn("listening");
    setMic("mic live");
    logLine(`session ready: ${payload.session_id}`);
    return;
  }

  if (payload.type === "turn.state") {
    setTurn(payload.state || "unknown");
    return;
  }

  if (payload.type === "transcript.final") {
    transcriptEl.textContent = payload.text || "-";
    return;
  }

  if (payload.type === "audio.chunk") {
    if (payload.audio_b64) schedulePlayback(payload.audio_b64);
    return;
  }

  if (payload.type === "audio.clear") {
    clearPlayback();
    return;
  }

  if (payload.type === "error") {
    logLine(`server error (${payload.code}): ${payload.message}`);
  }
}

async function startSession() {
  if (running) return;

  toggleBtn.disabled = true;
  toggleBtn.textContent = "Starting...";

  try {
    const sessionResp = await fetch("/v1/realtime/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });

    if (!sessionResp.ok) {
      const err = await sessionResp.json().catch(() => ({}));
      throw new Error(err.error || `session endpoint failed (${sessionResp.status})`);
    }

    const session = await sessionResp.json();
    const wsUrl = session.ws_url;

    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    await audioCtx.resume();
    outputGain = audioCtx.createGain();
    outputGain.gain.value = 1;
    outputGain.connect(audioCtx.destination);
    playbackCursor = audioCtx.currentTime;

    await attachMicrophone();

    ws = new WebSocket(wsUrl);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      running = true;
      ws.send(JSON.stringify({
        type: "session.start",
        codec: "pcm_s16le",
        sample_rate_hz: 16000,
      }));
      setConnection("connected");
      setMic("mic live");
      logLine("websocket open");
      toggleBtn.textContent = "Stop Session";
      toggleBtn.disabled = false;
      muteBtn.disabled = false;
    };

    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        handleServerMessage(payload);
      } catch {
        logLine("received non-JSON server message");
      }
    };

    ws.onclose = async () => {
      logLine("websocket closed");
      await stopSession(false);
    };

    ws.onerror = () => {
      logLine("websocket error");
    };
  } catch (err) {
    logLine(`start failed: ${err.message}`);
    await stopSession(false);
  } finally {
    if (!running) {
      toggleBtn.textContent = "Start Session";
      toggleBtn.disabled = false;
    }
  }
}

async function stopSession(sendStop = true) {
  const shouldCleanup = running || ws || audioCtx || mediaStream;
  running = false;

  if (ws) {
    try {
      if (sendStop && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "session.stop" }));
        ws.close(1000, "client stop");
      } else if (ws.readyState === WebSocket.CONNECTING) {
        ws.close();
      }
    } catch {
      // no-op
    }
    ws.onopen = null;
    ws.onmessage = null;
    ws.onclose = null;
    ws.onerror = null;
    ws = null;
  }

  await cleanupAudio();

  if (shouldCleanup) {
    setConnection("disconnected");
    setTurn("idle");
    setMic("mic off");
    toggleBtn.textContent = "Start Session";
    toggleBtn.disabled = false;
    muteBtn.disabled = true;
  }
}

toggleBtn.addEventListener("click", async () => {
  if (running) {
    await stopSession(true);
    return;
  }
  await startSession();
});

muteBtn.addEventListener("click", () => {
  muted = !muted;
  if (outputGain) {
    outputGain.gain.value = muted ? 0 : 1;
  }
  muteBtn.textContent = muted ? "Unmute Playback" : "Mute Playback";
});

window.addEventListener("beforeunload", () => {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: "session.stop" }));
  }
});

logLine("ready. tap Start Session to begin.");
