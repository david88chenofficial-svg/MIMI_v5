const selectedFiles = {
  spec: [],
  background: [],
  image: [],
  plannerOutput: [],
  taskBreaker: [],
  subtaskFiles: [],
};
let modelCatalog = null;
let selectedLevel = null;
let currentRunActive = false;
let hasRunStarted = false;
const defaultModels = {
  planner: "gpt-5.1",
  task_breaker: "gpt-5-nano",
  coder: "gpt-5-nano",
  verifier: "gpt-5-nano",
  supervisor: "gpt-5-nano",
  coder_secretary: "gpt-5-nano",
};
const defaultAgentSettings = {
  planner: {
    reasoning_effort: "medium",
    verbosity: "high",
  },
};
const settingPreference = {
  reasoning_effort: ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
  verbosity: ["default", "low", "medium", "high"],
};
const levelConfig = {
  1: {
    title: "From specification",
    requiredKey: "spec",
  },
  2: {
    title: "From plan",
    requiredKey: "plannerOutput",
  },
  3: {
    title: "From subtasks",
    requiredKey: "taskBreaker",
  },
};

const form = document.getElementById("input-form");
const levelSelector = document.getElementById("level-selector");
const workspace = document.getElementById("workspace");
const changeLevelButton = document.getElementById("change-level");
const selectedLevelLabel = document.getElementById("selected-level-label");
const workspaceTitle = document.getElementById("workspace-title");
const runControls = document.getElementById("run-controls");
const runResults = document.getElementById("run-results");
const abortButton = document.getElementById("abort-button");
const statusEl = document.getElementById("status");
const runButton = document.getElementById("run-button");
const runState = document.getElementById("run-state");
const startSubtaskInput = document.getElementById("start-subtask");
const progressLabel = document.getElementById("progress-label");
const progressCount = document.getElementById("progress-count");
const progressFill = document.getElementById("progress-fill");
const plannerOutput = document.getElementById("planner-output");
const taskBreakerOutput = document.getElementById("task-breaker-output");
const coderOutput = document.getElementById("coder-output");
const voiceIntake = document.getElementById("voice-intake");
const voiceStatus = document.getElementById("voice-status");
const voiceDevice = document.getElementById("voice-device");
const voiceTimer = document.getElementById("voice-timer");
const voiceMeter = document.getElementById("voice-meter");
const voiceStartButton = document.getElementById("voice-start");
const voiceStopButton = document.getElementById("voice-stop");
const voiceCancelButton = document.getElementById("voice-cancel");
const voiceResult = document.getElementById("voice-result");
const voiceResultPath = document.getElementById("voice-result-path");
const voiceResultPreview = document.getElementById("voice-result-preview");
const voiceRecordAgainButton = document.getElementById("voice-record-again");
const popPhoneLaunch = new URLSearchParams(window.location.search).get("source") === "pop-phone";
const maxVoiceSeconds = 300;
let voiceStream = null;
let voiceAudioContext = null;
let voiceSource = null;
let voiceProcessor = null;
let voiceSilentGain = null;
let voiceChunks = [];
let voiceStartedAt = null;
let voiceTimerInterval = null;
let voiceMaxDurationTimeout = null;
let voiceRecording = false;

function setStatus(message, state = "idle") {
  statusEl.textContent = message;
  runState.textContent = state === "running" ? "Running" : state === "error" ? "Error" : "Idle";
  runState.dataset.state = state;
  runState.hidden = state === "idle";
}

function setVoiceStatus(message, state = "idle") {
  voiceStatus.textContent = message;
  voiceMeter.dataset.state = state;
}

function voiceElapsedSeconds() {
  return voiceStartedAt ? Math.floor((Date.now() - voiceStartedAt) / 1000) : 0;
}

function renderVoiceTimer() {
  const seconds = voiceElapsedSeconds();
  const minutesPart = String(Math.floor(seconds / 60)).padStart(2, "0");
  const secondsPart = String(seconds % 60).padStart(2, "0");
  voiceTimer.textContent = `${minutesPart}:${secondsPart}`;
}

function clearVoiceTimers() {
  if (voiceTimerInterval) {
    window.clearInterval(voiceTimerInterval);
    voiceTimerInterval = null;
  }
  if (voiceMaxDurationTimeout) {
    window.clearTimeout(voiceMaxDurationTimeout);
    voiceMaxDurationTimeout = null;
  }
}

function stopVoiceHardware() {
  clearVoiceTimers();
  if (voiceProcessor) {
    voiceProcessor.onaudioprocess = null;
    voiceProcessor.disconnect();
    voiceProcessor = null;
  }
  if (voiceSource) {
    voiceSource.disconnect();
    voiceSource = null;
  }
  if (voiceSilentGain) {
    voiceSilentGain.disconnect();
    voiceSilentGain = null;
  }
  if (voiceStream) {
    for (const track of voiceStream.getTracks()) {
      track.stop();
    }
    voiceStream = null;
  }
  if (voiceAudioContext) {
    voiceAudioContext.close().catch(() => {});
    voiceAudioContext = null;
  }
}

async function openPopPhoneMicrophone() {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("This browser does not support microphone recording.");
  }

  let stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  const devices = await navigator.mediaDevices.enumerateDevices();
  const popPhone = devices.find((device) => (
    device.kind === "audioinput"
    && /native union pop phone/i.test(device.label)
  ));
  if (!popPhone) {
    for (const track of stream.getTracks()) {
      track.stop();
    }
    throw new Error(
      "The Native Union POP Phone microphone was not found. "
      + "Reconnect it, allow microphone access, and try again.",
    );
  }

  const activeDeviceId = stream.getAudioTracks()[0]?.getSettings().deviceId;
  if (activeDeviceId !== popPhone.deviceId) {
    for (const track of stream.getTracks()) {
      track.stop();
    }
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: { exact: popPhone.deviceId },
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  }
  return { stream, label: popPhone.label };
}

async function startVoiceCapture() {
  if (voiceRecording) {
    return;
  }
  voiceStartButton.disabled = true;
  voiceStartButton.hidden = true;
  voiceStopButton.disabled = true;
  voiceCancelButton.disabled = false;
  voiceDevice.textContent = "";
  voiceTimer.textContent = "00:00";
  setVoiceStatus("Connecting to the POP Phone microphone...", "processing");

  try {
    const selectedMicrophone = await openPopPhoneMicrophone();
    voiceStream = selectedMicrophone.stream;
    voiceDevice.textContent = selectedMicrophone.label;
    voiceAudioContext = new AudioContext();
    voiceSource = voiceAudioContext.createMediaStreamSource(voiceStream);
    voiceProcessor = voiceAudioContext.createScriptProcessor(4096, 1, 1);
    voiceSilentGain = voiceAudioContext.createGain();
    voiceSilentGain.gain.value = 0;
    voiceChunks = [];
    voiceProcessor.onaudioprocess = (event) => {
      if (voiceRecording) {
        voiceChunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
      }
    };
    voiceSource.connect(voiceProcessor);
    voiceProcessor.connect(voiceSilentGain);
    voiceSilentGain.connect(voiceAudioContext.destination);
    await voiceAudioContext.resume();

    voiceRecording = true;
    voiceStartedAt = Date.now();
    renderVoiceTimer();
    voiceTimerInterval = window.setInterval(renderVoiceTimer, 250);
    voiceMaxDurationTimeout = window.setTimeout(() => {
      stopVoiceCapture();
    }, maxVoiceSeconds * 1000);
    voiceStopButton.disabled = false;
    setVoiceStatus("Listening - speak naturally, then press Stop.", "recording");
  } catch (error) {
    stopVoiceHardware();
    voiceRecording = false;
    voiceStartButton.disabled = false;
    voiceStartButton.hidden = false;
    voiceStopButton.disabled = true;
    setVoiceStatus(error.message, "error");
  }
}

function mergeVoiceChunks(chunks) {
  const totalLength = chunks.reduce((total, chunk) => total + chunk.length, 0);
  const merged = new Float32Array(totalLength);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.length;
  }
  return merged;
}

function resampleVoice(input, inputRate, outputRate = 16000) {
  if (inputRate <= outputRate) {
    return { samples: input, sampleRate: inputRate };
  }
  const ratio = inputRate / outputRate;
  const output = new Float32Array(Math.round(input.length / ratio));
  for (let index = 0; index < output.length; index += 1) {
    const start = Math.round(index * ratio);
    const end = Math.min(input.length, Math.round((index + 1) * ratio));
    let sum = 0;
    for (let sourceIndex = start; sourceIndex < end; sourceIndex += 1) {
      sum += input[sourceIndex];
    }
    output[index] = sum / Math.max(1, end - start);
  }
  return { samples: output, sampleRate: outputRate };
}

function writeAscii(view, offset, value) {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index));
  }
}

function encodeVoiceWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, samples.length * 2, true);
  let outputOffset = 44;
  for (const sample of samples) {
    const clamped = Math.max(-1, Math.min(1, sample));
    view.setInt16(
      outputOffset,
      clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff,
      true,
    );
    outputOffset += 2;
  }
  return new Blob([view], { type: "audio/wav" });
}

function blobAsDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Could not read the voice recording."));
    reader.readAsDataURL(blob);
  });
}

async function createVoiceSpecification(wavBlob) {
  voiceStopButton.disabled = true;
  voiceCancelButton.disabled = true;
  setVoiceStatus(
    "The voice-intake agent is clarifying your instruction...",
    "processing",
  );
  try {
    const response = await fetch("/voice-spec", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audioData: await blobAsDataUrl(wavBlob) }),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.message || "Could not create the voice specification.");
    }

    const specFile = new File(
      [result.text],
      result.name || "voice_tool_spec.md",
      { type: "text/markdown" },
    );
    const specDropZone = document.querySelector('.drop-zone[data-key="spec"]');
    selectedFiles.spec = [specFile];
    renderFileList("spec", specDropZone);
    voiceResultPath.textContent = `Saved to ${result.path} using ${result.model}.`;
    voiceResultPreview.textContent = result.text;
    voiceIntake.hidden = true;
    document.title = "MIMI Inputs";
    selectLevel(1, { preserveFiles: true });
    voiceResult.hidden = false;
    setStatus("Voice specification ready - review it, then run MIMI.", "idle");
    updateReadyState({ updateStatus: false });
  } catch (error) {
    voiceCancelButton.disabled = false;
    voiceStartButton.disabled = false;
    voiceStartButton.hidden = false;
    setVoiceStatus(error.message, "error");
  }
}

async function stopVoiceCapture({ discard = false } = {}) {
  if (!voiceRecording) {
    if (discard) {
      stopVoiceHardware();
    }
    return;
  }
  voiceRecording = false;
  const chunks = voiceChunks;
  const sampleRate = voiceAudioContext?.sampleRate || 48000;
  const elapsed = voiceElapsedSeconds();
  stopVoiceHardware();
  voiceStopButton.disabled = true;

  if (discard) {
    return;
  }
  if (elapsed < 1 || !chunks.length) {
    voiceStartButton.disabled = false;
    voiceStartButton.hidden = false;
    setVoiceStatus("The recording was too short. Please try again.", "error");
    return;
  }

  const resampled = resampleVoice(mergeVoiceChunks(chunks), sampleRate);
  await createVoiceSpecification(
    encodeVoiceWav(resampled.samples, resampled.sampleRate),
  );
}

function showVoiceIntake() {
  clearSelectedFiles();
  selectedLevel = null;
  hasRunStarted = false;
  workspace.hidden = true;
  levelSelector.hidden = true;
  voiceResult.hidden = true;
  voiceIntake.hidden = false;
  document.title = "MIMI Voice Intake";
  voiceStartButton.hidden = true;
  voiceCancelButton.disabled = false;
  startVoiceCapture();
}

function leaveVoiceIntake() {
  stopVoiceCapture({ discard: true });
  voiceIntake.hidden = true;
  workspace.hidden = true;
  levelSelector.hidden = false;
  document.title = "MIMI Inputs";
}

function conciseStatus(state) {
  if (state.error) {
    return state.message || "Run failed";
  }
  if (state.message) {
    return state.message;
  }
  const labels = {
    starting: "Starting",
    planner: "Creating plan",
    plan_resume: "Loading plan",
    task_breaker: "Creating subtasks",
    resume: "Loading subtasks",
    coder: "Writing code",
    execution: "Running generated program",
    collecting: "Collecting outputs",
    verifier: "Checking results",
    documentation: "Updating documentation",
    recovery_analysis: "Diagnosing root cause",
    planner_revision: "Revising plan",
    task_breaker_revision: "Updating subtasks",
    integration: "Integrating accepted modules",
    integration_execution: "Running integrated tool",
    integration_verifier: "Checking integrated tool",
    integration_documentation: "Documenting integrated tool",
    integration_complete: "Integrated tool accepted",
    stopping: "Stopping",
    aborted: "Aborted",
    complete: "Complete",
  };
  return labels[state.phase] || (state.running ? "Running" : "Ready");
}

function updateReadyState({ updateStatus = true } = {}) {
  if (!selectedLevel) {
    runButton.disabled = true;
    return;
  }

  const config = levelConfig[selectedLevel];
  const hasRequiredFile = selectedFiles[config.requiredKey].length > 0;
  runControls.hidden = !hasRequiredFile && !currentRunActive && !hasRunStarted;
  abortButton.hidden = !currentRunActive;
  runButton.hidden = currentRunActive;
  runButton.disabled = currentRunActive || !hasRequiredFile;
  changeLevelButton.disabled = currentRunActive;

  if (!currentRunActive && updateStatus) {
    setStatus("Ready", "idle");
  }
}

function levelIncludes(element, level) {
  return (element.dataset.levels || "")
    .split(",")
    .map((value) => Number.parseInt(value, 10))
    .includes(level);
}

function clearSelectedFiles() {
  for (const key of Object.keys(selectedFiles)) {
    selectedFiles[key] = [];
    const dropZone = document.querySelector(`.drop-zone[data-key="${key}"]`);
    if (!dropZone) {
      continue;
    }
    dropZone.querySelector("input").value = "";
    renderFileList(key, dropZone);
  }
  startSubtaskInput.value = "1";
}

function selectLevel(level, { preserveFiles = false } = {}) {
  if (!levelConfig[level] || currentRunActive) {
    return;
  }
  if (!preserveFiles) {
    clearSelectedFiles();
  }

  selectedLevel = level;
  levelSelector.hidden = true;
  workspace.hidden = false;

  for (const element of document.querySelectorAll(".level-only, .result-only")) {
    element.hidden = !levelIncludes(element, level);
  }

  const config = levelConfig[level];
  selectedLevelLabel.textContent = `Level ${level}`;
  workspaceTitle.textContent = config.title;
  if (!hasRunStarted) {
    runResults.hidden = true;
  }
  updateReadyState();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function returnToLevelSelector() {
  if (currentRunActive) {
    return;
  }
  clearSelectedFiles();
  hasRunStarted = false;
  selectedLevel = null;
  workspace.hidden = true;
  runControls.hidden = true;
  runResults.hidden = true;
  levelSelector.hidden = false;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function setSelectedFiles(key, files, dropZone) {
  const allowMultiple = dropZone.querySelector("input").multiple;
  const incomingFiles = Array.from(files || []);
  selectedFiles[key] = allowMultiple ? incomingFiles : incomingFiles.slice(0, 1);
  renderFileList(key, dropZone);
  updateReadyState();
}

function removeSelectedFile(key, index, dropZone) {
  selectedFiles[key].splice(index, 1);
  dropZone.querySelector("input").value = "";
  renderFileList(key, dropZone);
  updateReadyState();
}

function renderFileList(key, dropZone) {
  const files = selectedFiles[key];
  dropZone.querySelector(".file-name").textContent = files.length
    ? `${files.length} file${files.length === 1 ? "" : "s"} selected`
    : "No file selected";
  dropZone.classList.toggle("has-file", files.length > 0);

  const fileList = dropZone.querySelector(".file-list");
  fileList.innerHTML = files.map((file, index) => `
    <span class="file-chip">
      <span>${escapeHtml(file.name)}</span>
      <button type="button" data-remove-index="${index}" aria-label="Remove ${escapeHtml(file.name)}">Remove</button>
    </span>
  `).join("");

  for (const button of fileList.querySelectorAll("[data-remove-index]")) {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      removeSelectedFile(key, Number.parseInt(button.dataset.removeIndex, 10), dropZone);
    });
  }
}

function setupDropZone(dropZone) {
  const key = dropZone.dataset.key;
  const input = dropZone.querySelector("input");

  input.addEventListener("change", () => {
    setSelectedFiles(key, input.files, dropZone);
  });

  dropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropZone.classList.add("is-active");
  });

  dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("is-active");
  });

  dropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropZone.classList.remove("is-active");
    setSelectedFiles(key, event.dataTransfer.files, dropZone);
  });
}

function readDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function buildPayload() {
  if (!selectedLevel) {
    throw new Error("Choose a MIMI starting level first.");
  }

  return {
    runLevel: selectedLevel,
    spec: selectedLevel === 1 && selectedFiles.spec.length
      ? {
          name: selectedFiles.spec[0].name,
          text: await selectedFiles.spec[0].text(),
        }
      : null,
    background: selectedLevel === 1 && selectedFiles.background.length
      ? {
          name: selectedFiles.background[0].name,
          text: await selectedFiles.background[0].text(),
        }
      : null,
    images: await Promise.all(selectedFiles.image.map(async (file) => ({
      name: file.name,
      dataUrl: await readDataUrl(file),
    }))),
    plannerOutput: selectedLevel === 2 && selectedFiles.plannerOutput.length
      ? {
          name: selectedFiles.plannerOutput[0].name,
          text: await selectedFiles.plannerOutput[0].text(),
        }
      : null,
    taskBreaker: selectedLevel === 3 && selectedFiles.taskBreaker.length
      ? {
          name: selectedFiles.taskBreaker[0].name,
          text: await selectedFiles.taskBreaker[0].text(),
        }
      : null,
    subtaskFiles: selectedLevel === 3
      ? await Promise.all(selectedFiles.subtaskFiles.map(async (file) => ({
          name: file.name,
          dataUrl: await readDataUrl(file),
        })))
      : [],
    startSubtask: selectedLevel === 3
      ? (Number.parseInt(startSubtaskInput.value, 10) || 1)
      : 1,
    models: selectedModels(),
    settings: selectedSettings(),
  };
}

async function setupModelSelectors() {
  const response = await fetch("/model-catalog", { cache: "no-store" });
  modelCatalog = await response.json();

  for (const select of document.querySelectorAll("[data-model-agent]")) {
    const agent = select.dataset.modelAgent;
    const models = modelCatalog.agents[agent]?.models || Object.keys(modelCatalog.models);
    select.innerHTML = models.map((model) => (
      `<option value="${model}">${modelCatalog.models[model]?.label || model}</option>`
    )).join("");
    select.value = models.includes(defaultModels[agent]) ? defaultModels[agent] : models[0];
    select.addEventListener("change", () => updateSettingSelectors(agent));
  }

  for (const agent of Object.keys(defaultModels)) {
    updateSettingSelectors(agent, { useDefaults: true });
  }
}

function updateSettingSelectors(agent, { useDefaults = false } = {}) {
  const modelSelect = document.querySelector(`[data-model-agent="${agent}"]`);
  if (!modelSelect || !modelCatalog) {
    return;
  }
  const modelSpec = modelCatalog.models[modelSelect.value] || {};
  const agentSettings = modelCatalog.agents[agent]?.settings?.[modelSelect.value] || {};
  const settingLists = {
    reasoning_effort: agentSettings.reasoning_effort || modelSpec.reasoning_efforts || [],
    verbosity: agentSettings.verbosity || modelSpec.verbosity || [],
  };

  for (const select of document.querySelectorAll(`[data-setting-agent="${agent}"]`)) {
    const options = settingLists[select.dataset.settingName] || ["default"];
    const configuredDefault = defaultAgentSettings[agent]?.[select.dataset.settingName];
    const currentValue = useDefaults
      ? (
        (configuredDefault && options.includes(configuredDefault) && configuredDefault)
        || settingPreference[select.dataset.settingName].find((value) => options.includes(value))
      )
      : (select.value || "default");
    select.innerHTML = options.map((option) => (
      `<option value="${option}">${option}</option>`
    )).join("");
    select.value = options.includes(currentValue)
      ? currentValue
      : (
        settingPreference[select.dataset.settingName].find((value) => options.includes(value))
        || options[0]
      );
  }
}

function selectedModels() {
  const models = {};
  for (const select of document.querySelectorAll("[data-model-agent]")) {
    models[select.dataset.modelAgent] = select.value;
  }
  return models;
}

function selectedSettings() {
  const settings = {};
  for (const select of document.querySelectorAll("[data-setting-agent]")) {
    if (select.value === "default") {
      continue;
    }
    const agent = select.dataset.settingAgent;
    const settingName = select.dataset.settingName;
    settings[agent] = settings[agent] || {};
    settings[agent][settingName] = select.value;
  }
  return settings;
}

async function pollStatus() {
  try {
    const response = await fetch("/status", { cache: "no-store" });
    const state = await response.json();
    currentRunActive = Boolean(state.running);
    abortButton.hidden = !currentRunActive;
    runButton.hidden = currentRunActive;
    const hasRuntimeState = (
      state.running
      || (state.phase && state.phase !== "idle")
      || Boolean(state.planner_output)
      || Boolean(state.task_breaker?.subtasks?.length)
      || Boolean(state.coder_runs?.length)
    );
    if (hasRuntimeState && selectedLevel) {
      hasRunStarted = true;
      runResults.hidden = false;
      runControls.hidden = false;
    }
    setStatus(
      conciseStatus(state),
      state.error ? "error" : state.running ? "running" : "idle",
    );
    renderRunState(state);
    changeLevelButton.disabled = currentRunActive;

    if (state.running) {
      runButton.disabled = true;
      window.setTimeout(pollStatus, 2500);
      return;
    }

    updateReadyState({ updateStatus: false });
  } catch (error) {
    if (!document.hidden) {
      setStatus(`Connection to MIMI was lost: ${error.message}`, "error");
    }
    window.setTimeout(pollStatus, 2500);
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderRunState(state) {
  renderProgress(state.progress || {});
  renderPlanner(state.planner_output || "");
  renderTaskBreaker(state.task_breaker || {});
  renderCoderRuns(state.coder_runs || []);
}

function renderProgress(progress) {
  const current = progress.current || 0;
  const total = progress.total || 0;
  const percent = Math.max(0, Math.min(100, progress.percent || 0));
  progressLabel.textContent = progress.label || (total ? "Tasks" : "Starting");
  progressCount.textContent = `${current} / ${total}`;
  progressFill.style.width = `${percent}%`;
}

function renderPlanner(text) {
  plannerOutput.textContent = text || "No plan yet.";
}

function renderTaskBreaker(taskBreaker) {
  const subtasks = taskBreaker.subtasks || [];
  if (!subtasks.length) {
    taskBreakerOutput.innerHTML = '<div class="empty-state">No subtasks yet.</div>';
    return;
  }

  taskBreakerOutput.innerHTML = subtasks.map((task) => `
    <article class="subtask-item">
      <div class="subtask-title">
        <span>Task ${escapeHtml(task.number)}${task.task_id ? ` - ${escapeHtml(task.task_id)}` : ""}</span>
        <span class="badge">${escapeHtml(task.status || (task.requires_coding ? "pending" : "no code"))}${task.attempt_count ? ` - ${escapeHtml(task.attempt_count)}/${escapeHtml(task.max_attempts || 3)} attempts` : ""}</span>
      </div>
      <div class="subtask-summary">${escapeHtml(task.summary)}</div>
    </article>
  `).join("");
}

function renderCoderRuns(runs) {
  if (!runs.length) {
    coderOutput.innerHTML = '<div class="empty-state">No code yet.</div>';
    return;
  }

  const grouped = new Map();
  for (const run of runs) {
    const key = run.subtask_number;
    if (!grouped.has(key)) {
      grouped.set(key, []);
    }
    grouped.get(key).push(run);
  }

  coderOutput.innerHTML = Array.from(grouped.entries()).map(([subtaskNumber, subtaskRuns]) => {
    const latest = subtaskRuns[subtaskRuns.length - 1];
    return `
      <details class="coder-item" open>
        <summary>
          <div class="coder-title">
            <span>Task ${escapeHtml(subtaskNumber)}</span>
            <span class="badge">${escapeHtml(latest.verifier_verdict || "pending")}</span>
          </div>
        </summary>
        <div class="coder-body">
          ${subtaskRuns.map(renderCoderAttempt).join("")}
        </div>
      </details>
    `;
  }).join("");
}

function renderCoderAttempt(run) {
  const artifacts = run.artifacts || { images: [], texts: [] };
  return `
    <details class="coder-item" open>
      <summary>
        <div class="coder-title">
          <span>Attempt ${escapeHtml(run.attempt)} - return code ${escapeHtml(run.return_code)}</span>
          <span class="badge">${escapeHtml(run.code_path || "code")}</span>
        </div>
      </summary>
      <div class="coder-body">
        <section class="artifact-section">
          <h3>Code</h3>
          <pre class="code-block">${escapeHtml(run.coder_output || "")}</pre>
        </section>
        <section class="artifact-section">
          <h3>Output</h3>
          <pre class="terminal-block">${escapeHtml(formatExecutionOutput(run))}</pre>
        </section>
        <section class="artifact-section">
          <h3>Plots</h3>
          ${renderPlots(artifacts.images || [])}
        </section>
        <section class="artifact-section">
          <h3>Text</h3>
          ${renderTexts(artifacts.texts || [])}
        </section>
      </div>
    </details>
  `;
}

function formatExecutionOutput(run) {
  const stdout = run.stdout || "";
  const stderr = run.stderr || "";
  if (!stdout && !stderr) {
    return "No stdout or stderr captured.";
  }
  return `STDOUT:\n${stdout || "[empty]"}\n\nSTDERR:\n${stderr || "[empty]"}`;
}

function renderPlots(images) {
  if (!images.length) {
    return '<div class="empty-state">No plots recorded for this attempt.</div>';
  }

  return `
    <div class="plot-grid">
      ${images.map((image) => `
        <figure class="plot-card">
          ${image.data_url ? `<img src="${escapeHtml(image.data_url)}" alt="${escapeHtml(image.description || image.name)}">` : ""}
          <figcaption class="artifact-caption">
            <strong>${escapeHtml(image.name)}</strong><br>
            ${escapeHtml(image.description || image.path || "")}
          </figcaption>
        </figure>
      `).join("")}
    </div>
  `;
}

function renderTexts(texts) {
  if (!texts.length) {
    return '<div class="empty-state">No txt files recorded for this attempt.</div>';
  }

  return texts.map((text) => `
    <details class="coder-item">
      <summary>
        <div class="coder-title">
          <span>${escapeHtml(text.name)}</span>
          <span class="badge">txt</span>
        </div>
      </summary>
      <div class="coder-body">
        <div class="artifact-caption">${escapeHtml(text.description || text.path || "")}</div>
        <pre class="txt-block">${escapeHtml(text.content || "")}</pre>
      </div>
    </details>
  `).join("");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  hasRunStarted = true;
  runControls.hidden = false;
  runResults.hidden = false;
  runButton.disabled = true;
  setStatus("Starting", "running");

  try {
    const response = await fetch("/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(await buildPayload()),
    });
    const result = await response.json();
    setStatus(response.ok ? "Starting" : result.message || "Could not start", response.ok ? "running" : "error");
    if (response.ok) {
      currentRunActive = true;
      abortButton.hidden = false;
      abortButton.disabled = false;
      runButton.hidden = true;
      changeLevelButton.disabled = true;
      pollStatus();
    } else {
      updateReadyState({ updateStatus: false });
    }
  } catch (error) {
    setStatus(`Upload failed: ${error.message}`, "error");
    updateReadyState({ updateStatus: false });
  }
});

abortButton.addEventListener("click", async () => {
  abortButton.disabled = true;
  setStatus("Stopping", "running");
  try {
    const response = await fetch("/abort", {
      method: "POST",
      cache: "no-store",
    });
    const result = await response.json();
    if (!response.ok) {
      currentRunActive = false;
      setStatus(result.message || "Could not stop run", "error");
      updateReadyState({ updateStatus: false });
      return;
    }
    window.setTimeout(pollStatus, 250);
  } catch (error) {
    abortButton.disabled = false;
    setStatus(`Could not stop: ${error.message}`, "error");
  }
});

for (const dropZone of document.querySelectorAll(".drop-zone")) {
  setupDropZone(dropZone);
}

for (const button of document.querySelectorAll("[data-level]")) {
  button.addEventListener("click", () => {
    selectLevel(Number.parseInt(button.dataset.level, 10));
  });
}

changeLevelButton.addEventListener("click", returnToLevelSelector);
voiceStartButton.addEventListener("click", startVoiceCapture);
voiceStopButton.addEventListener("click", () => stopVoiceCapture());
voiceCancelButton.addEventListener("click", leaveVoiceIntake);
voiceRecordAgainButton.addEventListener("click", showVoiceIntake);

setupModelSelectors().catch((error) => {
  setStatus(`Could not load model catalog: ${error.message}`, "error");
});

const browserClientId = (
  globalThis.crypto?.randomUUID?.()
  || `mimi-${Date.now()}-${Math.random().toString(16).slice(2)}`
);
let pageIsClosing = false;

async function maintainClientSession() {
  while (!pageIsClosing) {
    try {
      const response = await fetch(
        `/client-session?clientId=${encodeURIComponent(browserClientId)}`,
        { cache: "no-store" },
      );
      await response.text();
    } catch {
      // A brief connection loss is retried while this page remains open.
    }
    if (!pageIsClosing) {
      await new Promise((resolve) => window.setTimeout(resolve, 500));
    }
  }
}

async function sendHeartbeat() {
  try {
    await fetch("/heartbeat", {
      method: "POST",
      cache: "no-store",
      keepalive: true,
    });
  } catch {
    // The server may already be shutting down after the page closes.
  }
}

maintainClientSession();
sendHeartbeat();
const heartbeatTimer = window.setInterval(sendHeartbeat, 3000);
window.addEventListener("pagehide", () => {
  pageIsClosing = true;
  voiceRecording = false;
  stopVoiceHardware();
  window.clearInterval(heartbeatTimer);
  navigator.sendBeacon(
    `/disconnect?clientId=${encodeURIComponent(browserClientId)}`,
    "closed",
  );
});

if (popPhoneLaunch) {
  showVoiceIntake();
}
pollStatus();
