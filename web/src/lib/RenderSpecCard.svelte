<script>
  // One native form (render_spec, issue 428) in the transcript: loads it from its Host, draws it,
  // keeps the draft and sends the one answer through the form path, as the iOS app's form owner
  // does. The drawing is render-spec-draw.js; the rules are @core/render-spec.js.
  import { onDestroy } from 'svelte';
  import {
    acceptedSubmission, answeredLine, carrying, matchesIdentity, newSubmissionId, readRenderSpec, refusalMeaning,
  } from '@core/render-spec.js';
  import { mountForm } from './render-spec-draw.js';
  import { clog } from './net.js';
  import { t } from '../stores/i18n.svelte.js';

  let { row } = $props();

  // loading | form | newer | unreadable | failed
  let phase = $state('loading');
  // 'Answered · <button label>' once answered (a form this build cannot read: 'Answered').
  let shownForm = $state(null);
  let answered = $derived(shownForm ? answeredLine(shownForm.form, shownForm.accepted, t) : '');
  let status = $derived(answered || (row.status === 'completed' ? t('form.answered') : ''));
  let host = $state(null);
  let pending = null;
  let controller = null;
  let form = null;
  let loadedFor = '';
  let submissionId = newSubmissionId();
  let saveTimer = 0;
  let gone = false;

  const draftKey = version => `clarp.render-spec-draft:${row.artifact_id}:${version}`;
  function loadDraft(version) {
    try { const raw = localStorage.getItem(draftKey(version)); return raw ? JSON.parse(raw) : null; } catch (_) { return null; }
  }
  function saveDraft(values) {
    if (!form) return;
    const version = form.version;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      try { localStorage.setItem(draftKey(version), JSON.stringify(values)); } catch (_) {}
    }, 400);
  }
  function clearDraft(version) {
    clearTimeout(saveTimer);
    try { localStorage.removeItem(draftKey(version)); } catch (_) {}
  }

  async function digest(bytes) {
    const subtle = globalThis.crypto && globalThis.crypto.subtle;
    if (!subtle) return null;
    const hash = new Uint8Array(await subtle.digest('SHA-256', bytes));
    return Array.from(hash, b => b.toString(16).padStart(2, '0')).join('');
  }

  // Each load replaces the last; only the newest may draw.
  let loadSeq = 0;
  async function load({ typed = null, note = '' } = {}) {
    const seq = ++loadSeq;
    if (!typed) phase = 'loading';
    try {
      const r = await fetch('/artifacts/' + encodeURIComponent(row.artifact_id));
      if (!r.ok) throw new Error('status ' + r.status);
      const item = (await r.json()).artifact;
      if (seq !== loadSeq || gone) return;
      if (!item || item.type !== 'render_spec' || item.artifact_id !== row.artifact_id) throw new Error('not this form');
      // A row without the payload: this Host does not take this client's answers.
      if (!item.spec) { phase = 'newer'; return; }
      const reading = readRenderSpec(item);
      if (reading.kind !== 'form') {
        clog('renderSpec', `event=read outcome=${reading.kind}`);
        phase = reading.kind;
        return;
      }
      const sha = globalThis.crypto && globalThis.crypto.subtle ? digest : null;
      if (!(await matchesIdentity(reading.form, item.content_identity, sha))) {
        clog('renderSpec', 'event=read outcome=identity-mismatch');
        phase = 'unreadable';
        return;
      }
      if (seq !== loadSeq || gone) return;
      form = reading.form;
      const accepted = acceptedSubmission(item.submission);
      if (accepted) clearDraft(accepted.version);
      const values = accepted ? {} : carrying(form, typed || loadDraft(form.version) || {});
      show({ form, values, accepted, note });
    } catch (e) {
      if (seq !== loadSeq || gone) return;
      clog('renderSpec', 'event=read outcome=failed');
      phase = 'failed';
    }
  }

  function show(content) {
    phase = 'form';
    shownForm = content;
    pending = content;
    if (host) draw();
  }
  function draw() {
    if (!pending || !host) return;
    const { form: f, values, accepted, note } = pending;
    pending = null;
    controller = mountForm(host, { form: f, values, accepted, onSubmit: submit, onValuesChanged: saveDraft });
    if (note) controller.setSending(false, note);
  }
  $effect(() => { if (host && pending) draw(); });

  async function submit(answers) {
    if (!form || !controller) return;
    const f = form;
    controller.setSending(true);
    const body = JSON.stringify({ submission_id: submissionId, version: f.version, answers });
    let r;
    try {
      r = await fetch('/artifacts/' + encodeURIComponent(row.artifact_id) + '/submit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body,
      });
    } catch (_) {
      // Outcome unknown: the same id goes out again on the next press.
      clog('renderSpec', 'event=receipt outcome=unknown');
      controller?.setSending(false, t('form.send_failed'));
      return;
    }
    let data = null;
    try { data = await r.json(); } catch (_) {}
    if (gone) return;
    if (r.ok && data && data.accepted === true) {
      clearDraft(f.version);
      clog('renderSpec', `event=receipt outcome=accepted delivery=${data.delivery_status || ''}`);
      show({
        form: f, values: {}, note: '',
        accepted: {
          submissionId, version: f.version, intent: answers.intent, state: answers.state,
          acceptedAt: Date.now(), deliveryStatus: data.delivery_status || 'pending',
        },
      });
      return;
    }
    const refusal = data && data.error && typeof data.error === 'object' ? data.error : null;
    clog('renderSpec', `event=receipt outcome=refused code=${refusal ? refusal.code : r.status}`);
    if (!refusal) {
      controller.setSending(false, t('form.send_failed'));
      return;
    }
    // Whatever goes out next is a new attempt.
    submissionId = newSubmissionId();
    switch (refusalMeaning(refusal.code)) {
      case 'reopen':
        load({ typed: controller.values(), note: t('form.changed') });
        break;
      case 'answered': case 'reused':
        load();
        break;
      case 'closed':
        clearDraft(f.version);
        controller.close(t('form.closed'));
        break;
      case 'too_old':
        phase = 'newer';
        break;
      case 'answer':
        controller.setSending(false, t('form.not_taken'));
        break;
      case 'host_fault':
        controller.setSending(false, t('form.host_fault'));
        break;
      default:
        controller.setSending(false, t('form.refused'));
    }
  }

  // Load once per artifact, and again when the Host says it changed (a new version, an answer
  // from another device) while nothing is being sent from here.
  $effect(() => {
    const stamp = `${row.artifact_id}:${row.updated_at}:${row.status}`;
    if (stamp === loadedFor) return;
    const first = !loadedFor;
    loadedFor = stamp;
    if (first) load();
    else load({ typed: controller ? controller.values() : null });
  });

  onDestroy(() => { gone = true; clearTimeout(saveTimer); });
</script>

<article class="turn rs-artifact" data-artifact-id={row.artifact_id} aria-label={row.title || t('form.kind')}>
  <header class="rs-head">
    <span class="rs-kind">{t('form.kind')}{#if status}<span class="rs-status">{status}</span>{/if}</span>
    {#if row.title}<h2 class="rs-title">{row.title}</h2>{/if}
    {#if row.summary}<p class="rs-summary">{row.summary}</p>{/if}
  </header>
  {#if phase === 'loading'}
    <div class="rs-notice">{t('form.loading')}</div>
  {:else if phase === 'newer'}
    <div class="rs-notice">{t('form.newer')}</div>
  {:else if phase === 'unreadable'}
    <div class="rs-notice">{t('form.unreadable')}</div>
  {:else if phase === 'failed'}
    <div class="rs-notice">
      {t('form.load_failed')}
      <button type="button" class="rs-retry start-btn" onclick={() => load()}>{t('form.retry')}</button>
    </div>
  {/if}
  <div class="rs-form" bind:this={host} hidden={phase !== 'form'}></div>
</article>
