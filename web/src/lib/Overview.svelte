<script>
  // Responsive mission control for live Chats and saved Contacts. The server's
  // session identity remains authoritative; display names are presentation.
  import ChatChoices from './ChatChoices.svelte';
  import {
    AVATAR_PALETTE, agentSnapshot, app, avatarUrl, DEFAULT_ROSTER, flash, isDesktop,
    isUserNotificationUnread, refreshSessions, setSession,
  } from '../stores/app.svelte.js';
  import {
    buildAgentOverview, formatRelativeActivity,
  } from '@core/agent-overview.js';
  import { labelFor, loadBackendCatalogue } from '../stores/backends.svelte.js';
  import { t } from '../stores/i18n.svelte.js';
  import { formatNumber, formatRelative } from './i18n/format.js';

  let {
    open = $bindable(), onStart, onRelaunch, onVoice, onOrchestrator, onChatSettings,
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
      actionError = t('overview.refresh_failed');
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
      tr: t,
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

  function avatarColor(name) {
    return AVATAR_PALETTE[name] || 'var(--ochre)';
  }

  // Labels come from the Host catalogue so a backend this build has never
  // seen still reads as its own name rather than a raw id.
  function backendLabel(value) {
    return value ? labelFor(value) : t('overview.agent');
  }

  function contextPercent(row) {
    if (!row.context_tokens || !row.context_window) return 0;
    return Math.min(100, Math.round((row.context_tokens / row.context_window) * 100));
  }

  function pathTail(path) {
    const parts = String(path || '').split('/').filter(Boolean);
    return parts.slice(-2).join('/') || t('overview.no_workspace');
  }

  async function refreshOverview(showToast = true) {
    if (refreshing) return;
    refreshing = true;
    actionError = '';
    try {
      await refreshSessions();
      if (showToast) flash(t('overview.refreshed'), 1200);
    } catch (_) {
      actionError = t('overview.refresh_failed');
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
        throw new Error(body.error || t('overview.action_failed', { action: label }));
      }
      await refreshSessions();
      flash(label, 1400);
    } catch (error) {
      actionError = error.message || t('overview.action_failed', { action: label });
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
        throw new Error(body.error || t('overview.schedule_toggle_failed'));
      }
      await refreshSessions();
      flash(t(sched.enabled ? 'overview.schedule_disabled' : 'overview.schedule_enabled'), 1400);
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
        throw new Error(body.error || t('overview.release_failed'));
      }
      confirmingRelease = '';
      expandedSession = '';
      await refreshSessions();
      flash(t('overview.released', { name: row.name }), 1400);
    } catch (error) {
      actionError = error.message || t('overview.release_failed_long');
    } finally {
      mutationKey = '';
    }
  }

  // When a chat was last active, in the app's language ("5 minutes ago").
  function activityWhen(seconds) {
    return formatRelativeActivity(seconds, Date.now() / 1000, { tr: t, relative: formatRelative });
  }

  function closeOverview() {
    confirmingRelease = '';
    actionError = '';
    open = false;
  }
</script>

{#if open}
  <section id="overview" class="overview" aria-label={t('overview.title')}>
    <div class="overview-atmosphere" aria-hidden="true"></div>
    <div class="overview-shell">
      <header class="overview-hero">
        <div class="overview-title-block">
          <div class="overview-title-line">
            <h1>{t('overview.title')}</h1>
            <span class="overview-live-mark" class:connected={app.conn === 'live'}>
              <span></span>{['live', 'connecting', 'dead'].includes(app.conn) ? t(`overview.conn.${app.conn}`) : app.conn}
            </span>
          </div>
        </div>
        <div class="overview-hero-actions">
          <span class="version-badge" title={t('overview.version')}>{app.version || 'v…'}</span>
          {#if isDesktop}<a href="/viz" class="overview-icon-btn" aria-label={t('overview.fleet_map')} title={t('overview.fleet_map')}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 7 12 3-7 8Z"/><circle cx="6" cy="7" r="3"/><circle cx="18" cy="10" r="3"/><circle cx="11" cy="18" r="3"/></svg>
          </a>{/if}
          <button id="overviewChatSettings" class="overview-icon-btn" aria-label={t('overview.chat_settings')}
                  title={t('overview.chat_settings')} onclick={onChatSettings}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v10H9l-5 4z"/><path d="M8 9h8M8 12h5"/></svg>
          </button>
          <button id="overviewOrchestrator" class="overview-icon-btn" aria-label={t('overview.automation_settings')}
                  title={t('overview.automation_settings')} onclick={onOrchestrator}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1m0-12.8-2.1 2.1m-8.6 8.6-2.1 2.1"/><circle cx="12" cy="12" r="3.5"/></svg>
          </button>
          <button id="overviewReload" class="overview-icon-btn" class:spinning={refreshing}
                  aria-label={t('overview.refresh')} title={t('overview.refresh')} onclick={() => refreshOverview()}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 8.3A7 7 0 0 1 18.7 9M5.3 15a7 7 0 0 0 12.6.7"/></svg>
          </button>
          <button id="overviewClose" class="overview-icon-btn primary" aria-label={t('overview.close')}
                  onclick={closeOverview}>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>
          </button>
        </div>
      </header>

      <div class="overview-ledger" aria-label={t('overview.summary')}>
        <div><strong>{overview.counts.chats}</strong><span>{t('overview.chats')}</span></div>
        <div class:signal={overview.counts.working > 0}>
          <strong>{overview.counts.working}</strong><span>{t('overview.working')}</span>
        </div>
        <div class:attention={overview.counts.attention > 0}>
          <strong>{overview.counts.attention}</strong><span>{t('overview.needs_attention')}</span>
        </div>
        <div><strong>{overview.counts.contacts}</strong><span>{t('overview.contacts')}</span></div>
      </div>

      <div class="overview-toolbar">
        <label class="overview-search">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="m16 16 4 4"/></svg>
          <span class="sr-only">{t('overview.search')}</span>
          <input bind:value={query} type="search" placeholder={t('overview.search_placeholder')} />
          {#if query}<button aria-label={t('overview.clear_search')} onclick={() => query = ''}>×</button>{/if}
        </label>
        <div class="overview-tabs" role="tablist" aria-label={t('overview.sections')}>
          <button class:active={section === 'all'} onclick={() => section = 'all'}>{t('overview.tab.all')}</button>
          <button class:active={section === 'chats'} onclick={() => section = 'chats'}>{t('overview.chats')} <span>{overview.counts.chats}</span></button>
          <button class:active={section === 'contacts'} onclick={() => section = 'contacts'}>{t('overview.contacts')} <span>{overview.counts.contacts}</span></button>
          {#if overview.counts.archived > 0}
            <button class:active={section === 'archived'} onclick={() => section = 'archived'}>{t('overview.tab.archive')} <span>{overview.counts.archived}</span></button>
          {/if}
        </div>
      </div>

      {#if actionError}
        <div class="overview-error" role="alert"><span>{actionError}</span><button onclick={() => actionError = ''}>{t('overview.dismiss')}</button></div>
      {/if}

      <div class="overview-board" class:single-column={!visibleChats || !visibleContacts}>
        {#if visibleChats}
          <section class="overview-section chats-section" aria-labelledby="open-chats-title">
            <div class="overview-section-head">
              <div><span class="section-index">01</span><h2 id="open-chats-title">{t('overview.chats')}</h2></div>
              {#if overview.counts.working}<span>{t('overview.n_working', { count: overview.counts.working })}</span>{/if}
            </div>
            <div class="agent-card-grid">
              {#each overview.chats as row (row.key)}
                <article class="agent-card" class:current={row.isCurrent} class:busy={row.busy} class:expanded={expandedSession === row.session}
                         data-name={row.name} data-session={row.session}>
                  <button class="agent-card-open" onclick={() => pickRow(row)}>
                    <span class="agent-card-avatar" style="background-color:{avatarColor(row.name)};background-image:url('{rowAvatar(row)}')">
                      {#if !rowAvatar(row)}<span class="avatar-letter">{row.name.slice(0, 1)}</span>{/if}
                      <span class="agent-presence" class:busy={row.busy} class:unread={row.isUnread}></span>
                    </span>
                    <span class="agent-card-copy">
                      <span class="agent-card-heading"><strong>{row.name}</strong><span>{activityWhen(row.lastActivity)}</span></span>
                      <span class="agent-card-preview">{row.preview}</span>
                      <span class="agent-card-status" class:busy={row.busy} class:attention={row.isUnread || row.statusText}>
                        <i></i>{row.isCurrent ? t('overview.current', { state: row.stateLabel }) : row.stateLabel}
                      </span>
                    </span>
                  </button>

                  <div class="agent-card-tags" aria-label={t('overview.agent_context')}>
                    <span>{backendLabel(row.backend)}</span>
                    {#if row.model}<span title={row.model}>{row.model}</span>{/if}
                    <span title={row.cwd}>{pathTail(row.cwd)}</span>
                    {#if row.queued_turn_count > 0}<span class="warm">{t('overview.queued', { count: row.queued_turn_count })}</span>{/if}
                    {#if row.muted}<span>{t('overview.push_muted')}</span>{/if}
                  </div>

                  {#if row.context_tokens > 0 && row.context_window > 0}
                    <div class="agent-context" title={t('overview.context_tokens', { used: row.context_tokens, total: row.context_window })}>
                      <span style="width:{contextPercent(row)}%"></span><small>{t('overview.context_percent', { percent: formatNumber(contextPercent(row) / 100, { style: 'percent' }) })}</small>
                    </div>
                  {/if}

                  <div class="agent-card-actions">
                    <button class="solid" onclick={() => pickRow(row)}>{t('overview.open')}</button>
                    <button onclick={() => onVoice(row.session, row.name)}>{t('overview.voice')}</button>
                    <button onclick={() => onRelaunch(row.name, row.session)}>{t('overview.relaunch')}</button>
                    <button aria-expanded={expandedSession === row.session} onclick={() => expandedSession = expandedSession === row.session ? '' : row.session}>
                      {expandedSession === row.session ? t('overview.less') : t('overview.details')}
                    </button>
                  </div>

                  {#if expandedSession === row.session}
                    <div class="agent-card-detail">
                      <dl>
                        <div><dt>{t('overview.detail.session')}</dt><dd>{row.session}</dd></div>
                        <div><dt>{t('overview.detail.backend')}</dt><dd>{backendLabel(row.backend)}</dd></div>
                        <div><dt>{t('overview.detail.model')}</dt><dd>{row.model || t('overview.default')}</dd></div>
                        <div><dt>{t('overview.detail.effort')}</dt><dd>{row.effort || t('overview.default')}</dd></div>
                        <div><dt>{t('overview.detail.workspace')}</dt><dd title={row.cwd}>{row.cwd || '—'}</dd></div>
                        <div><dt>MCP</dt><dd>{row.mcp_servers?.length ? row.mcp_servers.join(', ') : t('overview.none')}</dd></div>
                      </dl>
                      <ChatChoices session={row.session} />
                      <div class="agent-toggle-row" aria-label={t('overview.quick_settings')}>
                        <button class:on={row.heartbeat_enabled} disabled={!!mutationKey} aria-pressed={row.heartbeat_enabled}
                                onclick={() => postAgentSetting(row, '/agent-heartbeat', { heartbeat_enabled: !row.heartbeat_enabled }, t(row.heartbeat_enabled ? 'overview.heartbeat_off' : 'overview.heartbeat_on'))}>
                          <span>{t('overview.heartbeat')}</span><i></i>
                        </button>
                        <button class:on={row.dreaming_enabled} disabled={!!mutationKey} aria-pressed={row.dreaming_enabled}
                                onclick={() => postAgentSetting(row, '/agent-dreaming', { dreaming_enabled: !row.dreaming_enabled }, t(row.dreaming_enabled ? 'overview.dreaming_off' : 'overview.dreaming_on'))}>
                          <span>{t('overview.dreaming')}</span><i></i>
                        </button>
                        <button class:on={!row.muted} disabled={!!mutationKey} aria-pressed={!row.muted}
                                onclick={() => postAgentSetting(row, '/agent-mute', { muted: !row.muted }, t(row.muted ? 'overview.push_on' : 'overview.push_off'))}>
                          <span>{t('overview.push')}</span><i></i>
                        </button>
                      </div>
                      {#if row.schedules?.length}
                        <div class="agent-schedules-block">
                          <span class="agent-schedules-heading">{t('overview.schedules')}</span>
                          {#each row.schedules as sched (sched.schedule_id)}
                            <div class="agent-schedule-row">
                              <div class="agent-schedule-meta">
                                <strong>{sched.name}</strong>
                                <code>{sched.cron_expression}</code>
                                <small>{sched.prompt}</small>
                              </div>
                              <button class="agent-toggle-btn" class:on={sched.enabled} disabled={!!mutationKey} aria-pressed={sched.enabled}
                                      onclick={() => toggleSchedule(sched)}>
                                <span>{sched.enabled ? t('overview.on') : t('overview.off')}</span><i></i>
                              </button>
                            </div>
                          {/each}
                        </div>
                      {/if}
                      <div class="agent-danger-row">
                        <button onclick={() => postAgentSetting(row, '/agent-archive', { archived: true }, t('overview.archived_toast', { name: row.name }))}>{t('overview.archive_chat')}</button>
                        {#if row.name !== 'Mike'}
                          {#if confirmingRelease === row.session}
                            <span>{t('overview.release_confirm')}</span><button onclick={() => confirmingRelease = ''}>{t('overview.cancel')}</button>
                            <button class="danger" disabled={!!mutationKey} onclick={() => releaseAgent(row)}>{t('overview.release')}</button>
                          {:else}
                            <button class="danger" onclick={() => confirmingRelease = row.session}>{t('overview.release_chat')}</button>
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
              <div><span class="section-index">02</span><h2 id="contacts-title">{t('overview.contacts')}</h2></div>
            </div>
            <div class="contact-stack">
              {#each overview.contacts as contact (contact.key)}
                <article class="contact-card" data-name={contact.name}>
                  <span class="contact-avatar" style="background-color:{avatarColor(contact.name)};background-image:url('{rowAvatar(contact)}')">
                    {#if !rowAvatar(contact)}<span class="avatar-letter">{contact.name.slice(0, 1)}</span>{/if}
                  </span>
                  <span class="contact-copy">
                    <span><strong>{contact.name}</strong><small>{contact.builtin ? t('overview.builtin') : t('overview.saved')}</small></span>
                    {#if contact.personality}<p>{contact.personality}</p>{/if}
                  </span>
                  <button onclick={() => onStart(contact.name)} aria-label={t('overview.start_with', { name: contact.name })}>{t('overview.start')}</button>
                </article>
              {/each}
            </div>
          </section>
        {/if}

        {#if visibleArchived}
          <section class="overview-section archive-section" aria-labelledby="archive-title">
            <div class="overview-section-head">
              <div><span class="section-index">03</span><h2 id="archive-title">{t('overview.archived')}</h2></div>
            </div>
            <div class="archive-list">
              {#each overview.archived as row (row.key)}
                <article>
                  <span class="contact-avatar" style="background-color:{avatarColor(row.name)};background-image:url('{rowAvatar(row)}')">
                    {#if !rowAvatar(row)}<span class="avatar-letter">{row.name.slice(0, 1)}</span>{/if}
                  </span>
                  <span><strong>{row.name}</strong><small>{row.preview} · {activityWhen(row.lastActivity)}</small></span>
                  <button disabled={!!mutationKey} onclick={() => postAgentSetting(row, '/agent-archive', { archived: false }, t('overview.restored', { name: row.name }))}>{t('overview.restore')}</button>
                </article>
              {/each}
            </div>
          </section>
        {/if}

        {#if !hasResults}
          <div class="overview-empty">
            <span>∅</span><h2>{t('overview.no_matches')}</h2>
            {#if query}<button onclick={() => query = ''}>{t('overview.clear_search')}</button>{/if}
          </div>
        {/if}
      </div>
    </div>
  </section>
{/if}
