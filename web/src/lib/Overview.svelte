<script>
  // Responsive mission control for live Chats and saved Contacts. The server's
  // session identity remains authoritative; display names are presentation.
  import {
    agentSnapshot, app, avatarUrl, DEFAULT_ROSTER, flash, isDesktop,
    isUserNotificationUnread, refreshSessions, setSession,
  } from '../stores/app.svelte.js';
  import {
    buildAgentOverview, formatRelativeActivity,
  } from '@core/agent-overview.js';
  import { labelFor, loadBackendCatalogue } from '../stores/backends.svelte.js';
  import AgentAvatar from './AgentAvatar.svelte';

  let {
    open = $bindable(), onStart, onRelaunch, onVoice, onOrchestrator,
  } = $props();

  $effect(() => {
    if (open) loadBackendCatalogue();
  });

  let query = $state('');
  let section = $state(isDesktop ? 'all' : 'chats');
  let expandedSession = $state('');
  let confirmingRelease = $state('');
  let mutationKey = $state('');
  let actionError = $state('');
  let refreshing = $state(false);

  $effect(() => {
    if (!open) return;
    // Keep this effect dependent only on `open`. Calling refreshOverview()
    // here would also subscribe it to the refreshing flag and create a fetch
    // loop every time the flag returned to false.
    refreshSessions().catch(() => {
      actionError = 'Could not refresh this Computer.';
    });
  });

  let overview = $derived.by(() => {
    app.tick;
    return buildAgentOverview({
      agentsBySession: app.agentsBySession,
      personas: agentSnapshot.personas,
      roster: agentSnapshot.roster.length ? agentSnapshot.roster : DEFAULT_ROSTER,
      availableSessions: app.availableSessions,
      currentSession: app.session,
      query,
      isUnread: isUserNotificationUnread,
    });
  });

  let visibleChats = $derived(section === 'all' || section === 'chats');
  let visibleContacts = $derived(section === 'all' || section === 'contacts');
  let visibleArchived = $derived(section === 'archived');
  let hasResults = $derived(
    (visibleChats && overview.chats.length > 0)
      || (visibleContacts && overview.contacts.length > 0)
      || (visibleArchived && overview.archived.length > 0));

  function rowAvatar(row) {
    return row.avatar_url || avatarUrl(row.name, row.session || '');
  }

  // Labels come from the Host catalogue so a backend this build has never
  // seen still reads as its own name rather than a raw id.
  function backendLabel(value) {
    return value ? labelFor(value) : 'Agent';
  }

  function contextPercent(row) {
    if (!row.context_tokens || !row.context_window) return 0;
    return Math.min(100, Math.round((row.context_tokens / row.context_window) * 100));
  }

  function pathTail(path) {
    const parts = String(path || '').split('/').filter(Boolean);
    return parts.slice(-2).join('/') || 'No workspace';
  }

  async function refreshOverview(showToast = true) {
    if (refreshing) return;
    refreshing = true;
    actionError = '';
    try {
      await refreshSessions();
      if (showToast) flash('Agents refreshed', 1200);
    } catch (_) {
      actionError = 'Could not refresh this Computer.';
    } finally {
      refreshing = false;
    }
  }

  function pickRow(row) {
    if (!row.session) return;
    setSession(row.session);
    open = false;
  }

  async function postAgentSetting(row, endpoint, payload, label) {
    if (!row.session || mutationKey) return;
    mutationKey = `${row.session}:${endpoint}`;
    actionError = '';
    try {
      const response = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: row.session, ...payload }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || `${label} failed`);
      }
      await refreshSessions();
      flash(label, 1400);
    } catch (error) {
      actionError = error.message || `${label} failed`;
    } finally {
      mutationKey = '';
    }
  }

  async function toggleSchedule(sched) {
    if (mutationKey) return;
    mutationKey = `sched:${sched.schedule_id}`;
    actionError = '';
    try {
      const response = await fetch('/agent-schedules/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ schedule_id: sched.schedule_id, enabled: !sched.enabled }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || 'Failed to toggle schedule');
      }
      await refreshSessions();
      flash(`Schedule ${sched.enabled ? 'disabled' : 'enabled'}`, 1400);
    } catch (error) {
      actionError = error.message;
    } finally {
      mutationKey = '';
    }
  }

  async function releaseAgent(row) {
    if (row.name === 'Mike' || mutationKey) return;
    mutationKey = `${row.session}:release`;
    actionError = '';
    try {
      const response = await fetch('/agents/' + encodeURIComponent(row.session), {
        method: 'DELETE',
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || 'Release failed');
      }
      confirmingRelease = '';
      expandedSession = '';
      await refreshSessions();
      flash(`${row.name} released`, 1400);
    } catch (error) {
      actionError = error.message || 'Could not release this Chat.';
    } finally {
      mutationKey = '';
    }
  }

  function closeOverview() {
    confirmingRelease = '';
    actionError = '';
    open = false;
  }
</script>

{#if open}
  <section id="overview" class="overview" aria-label="Agents">
    <div class="overview-atmosphere" aria-hidden="true"></div>
    <div class="overview-shell">
      <header class="overview-hero">
        <div class="overview-title-block">
          <span class="overview-kicker">Computer control surface</span>
          <div class="overview-title-line">
            <h1>Agents</h1>
            <span class="overview-live-mark" class:connected={app.conn === 'live'}>
              <span></span>{app.conn === 'live' ? 'Live' : app.conn}
            </span>
          </div>
          <p>Chats are running sessions. Contacts are identities ready for a new conversation.</p>
        </div>
        <div class="overview-hero-actions">
          <span class="version-badge" title="Server / client version">{app.version || 'v…'}</span>
          <button id="overviewOrchestrator" class="overview-icon-btn" aria-label="Automation settings"
                  title="Automation settings" onclick={onOrchestrator}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1m0-12.8-2.1 2.1m-8.6 8.6-2.1 2.1"/><circle cx="12" cy="12" r="3.5"/></svg>
          </button>
          <button id="overviewReload" class="overview-icon-btn" class:spinning={refreshing}
                  aria-label="Refresh agents" title="Refresh agents" onclick={() => refreshOverview()}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 8.3A7 7 0 0 1 18.7 9M5.3 15a7 7 0 0 0 12.6.7"/></svg>
          </button>
          <button id="overviewClose" class="overview-icon-btn primary" aria-label="Close"
                  onclick={closeOverview}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>
          </button>
        </div>
      </header>

      <div class="overview-ledger" aria-label="Agent summary">
        <div><strong>{overview.counts.chats}</strong><span>Open chats</span></div>
        <div class:signal={overview.counts.working > 0}>
          <strong>{overview.counts.working}</strong><span>Working now</span>
        </div>
        <div class:attention={overview.counts.attention > 0}>
          <strong>{overview.counts.attention}</strong><span>Need a look</span>
        </div>
        <div><strong>{overview.counts.contacts}</strong><span>Ready contacts</span></div>
      </div>

      <div class="overview-toolbar">
        <label class="overview-search">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="m16 16 4 4"/></svg>
          <span class="sr-only">Search agents</span>
          <input bind:value={query} type="search" placeholder="Search name, path, model, or message" />
          {#if query}<button aria-label="Clear search" onclick={() => query = ''}>×</button>{/if}
        </label>
        <div class="overview-tabs" role="tablist" aria-label="Agent sections">
          <button class:active={section === 'all'} onclick={() => section = 'all'}>All</button>
          <button class:active={section === 'chats'} onclick={() => section = 'chats'}>Chats <span>{overview.counts.chats}</span></button>
          <button class:active={section === 'contacts'} onclick={() => section = 'contacts'}>Contacts <span>{overview.counts.contacts}</span></button>
          {#if overview.counts.archived > 0}
            <button class:active={section === 'archived'} onclick={() => section = 'archived'}>Archive <span>{overview.counts.archived}</span></button>
          {/if}
        </div>
      </div>

      {#if actionError}
        <div class="overview-error" role="alert"><span>{actionError}</span><button onclick={() => actionError = ''}>Dismiss</button></div>
      {/if}

      <div class="overview-board" class:single-column={!visibleChats || !visibleContacts}>
        {#if visibleChats}
          <section class="overview-section chats-section" aria-labelledby="open-chats-title">
            <div class="overview-section-head">
              <div><span class="section-index">01</span><h2 id="open-chats-title">Open chats</h2></div>
              <span>{overview.counts.working ? `${overview.counts.working} in motion` : 'Quiet'}</span>
            </div>
            <div class="agent-card-grid">
              {#each overview.chats as row (row.key)}
                <article class="agent-card" class:current={row.isCurrent} class:busy={row.busy} class:expanded={expandedSession === row.session}
                         data-name={row.name} data-session={row.session}>
                  <button class="agent-card-open" onclick={() => pickRow(row)}>
                    <AgentAvatar class="agent-card-avatar" name={row.name} session={row.session} url={rowAvatar(row)}>
                      <span class="agent-presence" class:busy={row.busy} class:unread={row.isUnread}></span>
                    </AgentAvatar>
                    <span class="agent-card-copy">
                      <span class="agent-card-heading"><strong>{row.name}</strong><span>{formatRelativeActivity(row.lastActivity)}</span></span>
                      <span class="agent-card-preview">{row.preview}</span>
                      <span class="agent-card-status" class:busy={row.busy} class:attention={row.isUnread || row.statusText}>
                        <i></i>{row.isCurrent ? 'Current · ' : ''}{row.stateLabel}
                      </span>
                    </span>
                  </button>

                  <div class="agent-card-tags" aria-label="Agent context">
                    <span>{backendLabel(row.backend)}</span>
                    {#if row.model}<span title={row.model}>{row.model}</span>{/if}
                    <span title={row.cwd}>{pathTail(row.cwd)}</span>
                    {#if row.queued_turn_count > 0}<span class="warm">{row.queued_turn_count} queued</span>{/if}
                    {#if row.muted}<span>Push muted</span>{/if}
                  </div>

                  {#if row.context_tokens > 0 && row.context_window > 0}
                    <div class="agent-context" title="{row.context_tokens.toLocaleString()} of {row.context_window.toLocaleString()} context tokens">
                      <span style="width:{contextPercent(row)}%"></span><small>Context {contextPercent(row)}%</small>
                    </div>
                  {/if}

                  <div class="agent-card-actions">
                    <button class="solid" onclick={() => pickRow(row)}>Open</button>
                    <button onclick={() => onVoice(row.session, row.name)}>Voice</button>
                    <button onclick={() => onRelaunch(row.name, row.session)}>Relaunch</button>
                    <button aria-expanded={expandedSession === row.session} onclick={() => expandedSession = expandedSession === row.session ? '' : row.session}>
                      {expandedSession === row.session ? 'Less' : 'Details'}
                    </button>
                  </div>

                  {#if expandedSession === row.session}
                    <div class="agent-card-detail">
                      <dl>
                        <div><dt>Session</dt><dd>{row.session}</dd></div>
                        <div><dt>Backend</dt><dd>{backendLabel(row.backend)}</dd></div>
                        <div><dt>Model</dt><dd>{row.model || 'Provider default'}</dd></div>
                        <div><dt>Effort</dt><dd>{row.effort || 'Default'}</dd></div>
                        <div><dt>Workspace</dt><dd title={row.cwd}>{row.cwd || '—'}</dd></div>
                        <div><dt>MCP</dt><dd>{row.mcp_servers?.length ? row.mcp_servers.join(', ') : 'None requested'}</dd></div>
                      </dl>
                      <div class="agent-toggle-row" aria-label="Quick agent settings">
                        <button class:on={row.heartbeat_enabled} disabled={!!mutationKey} aria-pressed={row.heartbeat_enabled}
                                onclick={() => postAgentSetting(row, '/agent-heartbeat', { heartbeat_enabled: !row.heartbeat_enabled }, row.heartbeat_enabled ? 'Heartbeat off' : 'Heartbeat on')}>
                          <span>Heartbeat</span><i></i>
                        </button>
                        <button class:on={row.dreaming_enabled} disabled={!!mutationKey} aria-pressed={row.dreaming_enabled}
                                onclick={() => postAgentSetting(row, '/agent-dreaming', { dreaming_enabled: !row.dreaming_enabled }, row.dreaming_enabled ? 'Dreaming off' : 'Dreaming on')}>
                          <span>Dreaming</span><i></i>
                        </button>
                        <button class:on={!row.muted} disabled={!!mutationKey} aria-pressed={!row.muted}
                                onclick={() => postAgentSetting(row, '/agent-mute', { muted: !row.muted }, row.muted ? 'Push alerts on' : 'Push alerts muted')}>
                          <span>Push alerts</span><i></i>
                        </button>
                      </div>
                      {#if row.schedules?.length}
                        <div class="agent-schedules-block">
                          <span class="agent-schedules-heading">Scheduled Tasks</span>
                          {#each row.schedules as sched (sched.schedule_id)}
                            <div class="agent-schedule-row">
                              <div class="agent-schedule-meta">
                                <strong>{sched.name}</strong>
                                <code>{sched.cron_expression}</code>
                                <small>{sched.prompt}</small>
                              </div>
                              <button class="agent-toggle-btn" class:on={sched.enabled} disabled={!!mutationKey} aria-pressed={sched.enabled}
                                      onclick={() => toggleSchedule(sched)}>
                                <span>{sched.enabled ? 'On' : 'Off'}</span><i></i>
                              </button>
                            </div>
                          {/each}
                        </div>
                      {/if}
                      <div class="agent-danger-row">
                        <button onclick={() => postAgentSetting(row, '/agent-archive', { archived: true }, `${row.name} archived`)}>Archive chat</button>
                        {#if row.name !== 'Mike'}
                          {#if confirmingRelease === row.session}
                            <span>Release this Chat?</span><button onclick={() => confirmingRelease = ''}>Cancel</button>
                            <button class="danger" disabled={!!mutationKey} onclick={() => releaseAgent(row)}>Release</button>
                          {:else}
                            <button class="danger" onclick={() => confirmingRelease = row.session}>Release chat</button>
                          {/if}
                        {/if}
                      </div>
                    </div>
                  {/if}
                </article>
              {/each}
            </div>
          </section>
        {/if}

        {#if visibleContacts}
          <section class="overview-section contacts-section" aria-labelledby="contacts-title">
            <div class="overview-section-head">
              <div><span class="section-index">02</span><h2 id="contacts-title">Ready contacts</h2></div><span>Start fresh</span>
            </div>
            <div class="contact-stack">
              {#each overview.contacts as contact (contact.key)}
                <article class="contact-card" data-name={contact.name}>
                  <AgentAvatar class="contact-avatar" name={contact.name} url={rowAvatar(contact)} />
                  <span class="contact-copy">
                    <span><strong>{contact.name}</strong><small>{contact.builtin ? 'Built-in' : 'Saved'}</small></span>
                    <p>{contact.personality || 'Ready for a new workspace and conversation.'}</p>
                  </span>
                  <button onclick={() => onStart(contact.name)} aria-label="Start a chat with {contact.name}">Start</button>
                </article>
              {/each}
            </div>
          </section>
        {/if}

        {#if visibleArchived}
          <section class="overview-section archive-section" aria-labelledby="archive-title">
            <div class="overview-section-head">
              <div><span class="section-index">03</span><h2 id="archive-title">Archived chats</h2></div><span>Hidden from daily flow</span>
            </div>
            <div class="archive-list">
              {#each overview.archived as row (row.key)}
                <article>
                  <AgentAvatar class="contact-avatar" name={row.name} session={row.session} url={rowAvatar(row)} />
                  <span><strong>{row.name}</strong><small>{row.preview} · {formatRelativeActivity(row.lastActivity)}</small></span>
                  <button disabled={!!mutationKey} onclick={() => postAgentSetting(row, '/agent-archive', { archived: false }, `${row.name} restored`)}>Restore</button>
                </article>
              {/each}
            </div>
          </section>
        {/if}

        {#if !hasResults}
          <div class="overview-empty">
            <span>∅</span><h2>No matching agents</h2><p>Try a name, workspace, model, or clear the search.</p>
            {#if query}<button onclick={() => query = ''}>Clear search</button>{/if}
          </div>
        {/if}
      </div>
    </div>
  </section>
{/if}
