// needs: fake-host
// Profile (task plan, heartbeat, paged prompt history), settings status and
// TTS providers, voices and the orchestrator against the fake Host.
import QtQuick
import QtQuick.Window
import Clarp.Desktop

Window {
    width: 200; height: 200; visible: true
    property int failures: 0
    property int stage: 0
    property int ticks: 0
    function check(ok, what) {
        if (!ok) { failures++; console.log("FAIL " + what) } else console.log("ok   " + what)
    }
    function finish() {
        console.log(failures === 0 ? "PROBE_PASS" : "PROBE_FAIL " + failures)
        Qt.exit(failures === 0 ? 0 : 1)
    }
    AppController { id: app }
    Timer {
        id: ticker
        interval: 25; repeat: true; running: true
        onTriggered: {
            if (++ticks > 600) { check(false, "timed out at stage " + stage); ticker.stop(); finish(); return }
            try { step() } catch (e) { failures++; console.log("FAIL exception: " + e + "\n" + e.stack); ticker.stop(); finish() }
        }
    }
    function step() {
        if (stage === 0 && app.connected && app.agents.count === 2) {
            app.loadAgentProfile("rachel")
            check(app.profileLoading && app.profileSession === "rachel", "profile loading")
            stage = 1
        } else if (stage === 1 && !app.profileLoading && !app.profilePromptsLoading && app.profilePrompts.length === 20
                   && app.profileHeartbeat.interval_minutes !== undefined) {
            // The heartbeat is its own optional request, outside profileLoading.
            check(app.profileTaskPlan.title === "Ship it", "task plan")
            check(app.profileHeartbeat.interval_minutes === 30, "heartbeat status")
            check(app.profilePromptsHaveMore, "more prompts to page")
            app.loadPromptHistory("rachel", true)
            stage = 2
        } else if (stage === 2 && !app.profilePromptsLoading && app.profilePrompts.length === 30) {
            check(!app.profilePromptsHaveMore && app.profilePrompts[29].turn_id === "p30", "second page appended once")
            app.loadPromptHistory("rachel", true)
            check(!app.profilePromptsLoading, "no third page request")
            app.loadSettingsStatus()
            check(app.settingsStatusLoading, "settings status loading")
            stage = 3
        } else if (stage === 3 && !app.settingsStatusLoading) {
            check(app.diagnosticsHealth.status === "ok" && app.transcriptionCapabilities.providers.length === 1, "health and transcription")
            check(app.ttsProviderStatus.provider === "elevenlabs", "tts providers")
            app.setTtsProviders("openai", "", "")
            stage = 4
        } else if (stage === 4 && !app.settingsStatusLoading && app.ttsProviderStatus.provider === "openai") {
            check(app.ttsProviderStatus.fallback === "none", "an empty fallback is sent as none")
            app.loadVoices("rachel")
            stage = 5
        } else if (stage === 5 && !app.voicesLoading && app.voices.count === 2) {
            check(app.voiceBio === "Warm and clear", "voice bio")
            app.loadOrchestrator()
            stage = 6
        } else if (stage === 6 && !app.orchestratorLoading && app.orchestratorLastDecision !== "") {
            check(app.orchestratorLastDecision === "route: rachel (0.87)", "last decision: " + app.orchestratorLastDecision)
            app.saveOrchestrator(true, false, 2.0, "", "", "", 5)
            stage = 7
        } else if (stage === 7 && !app.orchestratorLoading && app.orchestratorSettings.enabled === true) {
            check(app.orchestratorSettings.provider === "openai" && app.orchestratorSettings.timeout_ms === 250, "defaults and clamps")
            check(app.orchestratorLastDecision === "No decisions logged yet.", "empty decision log")
            ticker.stop()
            finish()
        }
    }
}
