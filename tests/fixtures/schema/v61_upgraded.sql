-- user_version 125

-- index idx_agent_portraits_agent_created on agent_portraits
CREATE INDEX idx_agent_portraits_agent_created ON agent_portraits(agent_id, created_at) WHERE deleted_at IS NULL;

-- index idx_agent_portraits_live_content on agent_portraits
CREATE UNIQUE INDEX idx_agent_portraits_live_content ON agent_portraits(agent_id, sha256) WHERE deleted_at IS NULL;

-- index idx_agent_portraits_one_primary on agent_portraits
CREATE UNIQUE INDEX idx_agent_portraits_one_primary ON agent_portraits(agent_id) WHERE deleted_at IS NULL AND is_primary = 1;

-- index idx_agent_routing_recent on agent_routing_messages
CREATE INDEX idx_agent_routing_recent ON agent_routing_messages(agent_id, role, created_at DESC);

-- index idx_agent_routing_session_recent on agent_routing_messages
CREATE INDEX idx_agent_routing_session_recent ON agent_routing_messages(session, created_at DESC);

-- index idx_agent_schedules_next on agent_schedules
CREATE INDEX idx_agent_schedules_next ON agent_schedules(enabled, next_run_at);

-- index idx_agent_schedules_session on agent_schedules
CREATE INDEX idx_agent_schedules_session ON agent_schedules(session);

-- index idx_agents_parent on agents
CREATE INDEX idx_agents_parent ON agents(parent_agent_id) WHERE parent_agent_id IS NOT NULL;

-- index idx_agy_turn_authority_import on agy_turn_authority
CREATE INDEX idx_agy_turn_authority_import ON agy_turn_authority(agent_id, backend_session_id, assistant_start_ordinal, assistant_end_ordinal);

-- index idx_artifact_decisions_status on artifact_decisions
CREATE INDEX idx_artifact_decisions_status ON artifact_decisions(status, decision_id);

-- index idx_artifacts_agent_updated on artifacts
CREATE INDEX idx_artifacts_agent_updated ON artifacts(agent_id, updated_at DESC);

-- index idx_artifacts_created on artifacts
CREATE INDEX idx_artifacts_created ON artifacts(created_at DESC, artifact_id DESC);

-- index idx_artifacts_reference on artifacts
CREATE UNIQUE INDEX idx_artifacts_reference ON artifacts(agent_id, type, reference_id) WHERE reference_id != '' AND deleted_at IS NULL;

-- index idx_artifacts_session_updated on artifacts
CREATE INDEX idx_artifacts_session_updated ON artifacts(session, updated_at DESC);

-- index idx_artifacts_updated on artifacts
CREATE INDEX idx_artifacts_updated ON artifacts(updated_at DESC, artifact_id DESC);

-- index idx_backend_usage_fetched on backend_usage
CREATE INDEX idx_backend_usage_fetched ON backend_usage(fetched_at DESC);

-- index idx_background_job_events_job on background_job_events
CREATE INDEX idx_background_job_events_job ON background_job_events(job_id, event_id DESC);

-- index idx_background_jobs_active on background_jobs
CREATE INDEX idx_background_jobs_active ON background_jobs(status, updated_at DESC);

-- index idx_background_jobs_agent_active on background_jobs
CREATE INDEX idx_background_jobs_agent_active ON background_jobs(agent_id) WHERE terminal_at IS NULL;

-- index idx_background_jobs_computer on background_jobs
CREATE INDEX idx_background_jobs_computer ON background_jobs(owner_kind, computer_id, updated_at DESC);

-- index idx_background_jobs_session on background_jobs
CREATE INDEX idx_background_jobs_session ON background_jobs(session, updated_at DESC);

-- index idx_clips_agent_ts on clips
CREATE INDEX idx_clips_agent_ts ON clips(agent_id, created_at);

-- index idx_decision_applications_decision on decision_applications
CREATE INDEX idx_decision_applications_decision ON decision_applications(decision_id, created_at DESC);

-- index idx_decision_applications_trace on decision_applications
CREATE INDEX idx_decision_applications_trace ON decision_applications(trace_id, created_at DESC);

-- index idx_decision_deliveries_pending on decision_deliveries
CREATE INDEX idx_decision_deliveries_pending ON decision_deliveries(status, created_at);

-- index idx_decisions_created on decisions
CREATE INDEX idx_decisions_created ON decisions(created_at DESC);

-- index idx_decisions_question_hash_active on decisions
CREATE UNIQUE INDEX idx_decisions_question_hash_active ON decisions(question_hash) WHERE status = 'active';

-- index idx_decisions_type on decisions
CREATE INDEX idx_decisions_type ON decisions(decision_type, created_at DESC);

-- index idx_device_tokens_active on device_tokens
CREATE INDEX idx_device_tokens_active ON device_tokens(updated_at) WHERE disabled_at IS NULL;

-- index idx_dream_rounds_run_status on dream_rounds
CREATE INDEX idx_dream_rounds_run_status ON dream_rounds(run_id, status, round_index);

-- index idx_dream_rounds_stage on dream_rounds
CREATE INDEX idx_dream_rounds_stage ON dream_rounds(stage, completed_at DESC);

-- index idx_dream_runs_agent_status on dream_runs
CREATE INDEX idx_dream_runs_agent_status ON dream_runs(agent_id, status, started_at DESC);

-- index idx_dream_runs_started on dream_runs
CREATE INDEX idx_dream_runs_started ON dream_runs(started_at DESC);

-- index idx_dream_threads_run on dream_threads
CREATE INDEX idx_dream_threads_run ON dream_threads(run_id, thread_index);

-- index idx_form_submission_pending on form_submissions
CREATE INDEX idx_form_submission_pending ON form_submissions(status, created_at);

-- index idx_goal_delegations_delegate on goal_delegations
CREATE UNIQUE INDEX idx_goal_delegations_delegate ON goal_delegations(delegate_agent_id) WHERE status = 'active';

-- index idx_goal_delegations_principal on goal_delegations
CREATE UNIQUE INDEX idx_goal_delegations_principal ON goal_delegations(principal_agent_id) WHERE status = 'active';

-- index idx_goal_documents_latest on task_goal_documents
CREATE INDEX idx_goal_documents_latest ON task_goal_documents(plan_id, name, revision DESC);

-- index idx_goal_events_plan on goal_events
CREATE INDEX idx_goal_events_plan ON goal_events(plan_id, event_id);

-- index idx_goal_runs_goal on goal_runs
CREATE INDEX idx_goal_runs_goal ON goal_runs(goal_id, started_at DESC);

-- index idx_goal_runs_status on goal_runs
CREATE INDEX idx_goal_runs_status ON goal_runs(status, started_at DESC);

-- index idx_goals_status on goals
CREATE INDEX idx_goals_status ON goals(status, updated_at DESC);

-- index idx_heartbeat_accounting_agent_counted on heartbeat_accounting
CREATE INDEX idx_heartbeat_accounting_agent_counted ON heartbeat_accounting(agent_id, counted_at DESC);

-- index idx_html_form_events_artifact_seq on html_form_events
CREATE INDEX idx_html_form_events_artifact_seq ON html_form_events(artifact_id, seq);

-- index idx_janitor_attachments_agent on janitor_attachments
CREATE INDEX idx_janitor_attachments_agent ON janitor_attachments(agent_id, retired_at);

-- index idx_janitor_runs_agent on janitor_runs
CREATE INDEX idx_janitor_runs_agent ON janitor_runs(agent_id, created_at DESC);

-- index idx_janitor_runs_newest on janitor_runs
CREATE INDEX idx_janitor_runs_newest ON janitor_runs(agent_id, generation, status, finished_at);

-- index idx_janitor_runs_one_active on janitor_runs
CREATE UNIQUE INDEX idx_janitor_runs_one_active ON janitor_runs(agent_id) WHERE status IN ('queued', 'running');

-- index idx_judgment_decisions_created on judgment_decisions
CREATE INDEX idx_judgment_decisions_created ON judgment_decisions(created_at DESC);

-- index idx_judgment_decisions_site on judgment_decisions
CREATE INDEX idx_judgment_decisions_site ON judgment_decisions(site, created_at DESC);

-- index idx_media_assets_agent_created on media_assets
CREATE INDEX idx_media_assets_agent_created ON media_assets(agent_id, created_at DESC) WHERE deleted_at IS NULL;

-- index idx_media_assets_session_created on media_assets
CREATE INDEX idx_media_assets_session_created ON media_assets(session, created_at DESC) WHERE deleted_at IS NULL;

-- index idx_media_assets_sha on media_assets
CREATE INDEX idx_media_assets_sha ON media_assets(sha256);

-- index idx_messages_agent_revision on messages
CREATE INDEX idx_messages_agent_revision ON messages(agent_id, backend_session_id, revision, updated_at);

-- index idx_messages_agent_seq on messages
CREATE INDEX idx_messages_agent_seq ON messages(agent_id, backend_session_id, seq);

-- index idx_messages_agent_timestamp on messages
CREATE INDEX idx_messages_agent_timestamp ON messages(agent_id, timestamp);

-- index idx_messages_dashboard_activity on messages
CREATE INDEX idx_messages_dashboard_activity ON messages(agent_id, COALESCE(CAST((julianday(timestamp) - 2440587.5) * 86400000 AS INTEGER), updated_at) DESC, seq DESC, updated_at DESC) WHERE COALESCE(text, '') != '' AND COALESCE(tool_name, '') = '';

-- index idx_messages_final_rows on messages
CREATE INDEX idx_messages_final_rows ON messages(agent_id, backend_session_id) WHERE source_file LIKE 'final:%';

-- index idx_messages_log_order on messages
CREATE INDEX idx_messages_log_order ON messages(agent_id, backend_session_id, COALESCE(timestamp, ''), seq);

-- index idx_messages_pair_summary on messages
CREATE INDEX idx_messages_pair_summary ON messages(agent_id, sender_agent_id, role, revision, timestamp, seq) WHERE COALESCE(origin, 'user') = 'agent' AND COALESCE(sender_agent_id, '') != '' AND sender_agent_id != agent_id AND COALESCE(text, '') != '' AND COALESCE(tool_name, '') = '' AND role IN ('user', 'assistant');

-- index idx_messages_trace on messages
CREATE INDEX idx_messages_trace ON messages(trace_id) WHERE trace_id IS NOT NULL;

-- index idx_oracle_delegations_agent on oracle_delegations
CREATE INDEX idx_oracle_delegations_agent ON oracle_delegations(agent_id, created_at DESC);

-- index idx_oracle_delegations_delivery on oracle_delegations
CREATE INDEX idx_oracle_delegations_delivery ON oracle_delegations(owner_principal, delivered_at, updated_at DESC);

-- index idx_orchestrator_decisions_created on orchestrator_decisions
CREATE INDEX idx_orchestrator_decisions_created ON orchestrator_decisions(created_at DESC);

-- index idx_orchestrator_decisions_target on orchestrator_decisions
CREATE INDEX idx_orchestrator_decisions_target ON orchestrator_decisions(target_session, created_at DESC);

-- index idx_orchestrator_decisions_trace on orchestrator_decisions
CREATE INDEX idx_orchestrator_decisions_trace ON orchestrator_decisions(trace_id, created_at);

-- index idx_orchestrator_pending_status on orchestrator_pending_utterances
CREATE INDEX idx_orchestrator_pending_status ON orchestrator_pending_utterances(status, expires_at);

-- index idx_paired_devices_active on paired_devices
CREATE INDEX idx_paired_devices_active ON paired_devices(revoked_at, created_at DESC);

-- index idx_pairing_codes_expiry on pairing_codes
CREATE INDEX idx_pairing_codes_expiry ON pairing_codes(expires_at);

-- index idx_path_usage_favorites on path_usage
CREATE INDEX idx_path_usage_favorites ON path_usage(use_count DESC, last_used_at DESC);

-- index idx_personas_live_name on personas
CREATE INDEX idx_personas_live_name ON personas(name) WHERE deleted_at IS NULL;

-- index idx_personas_unique_live_name on personas
CREATE UNIQUE INDEX idx_personas_unique_live_name ON personas(name) WHERE deleted_at IS NULL;

-- index idx_podcast_episode on podcast_conversations
CREATE INDEX idx_podcast_episode ON podcast_conversations(artifact_id, created_at);

-- index idx_podcast_feedback_conversation on podcast_feedback
CREATE INDEX idx_podcast_feedback_conversation ON podcast_feedback(conversation_id, created_at);

-- index idx_podcast_feedback_target on podcast_feedback
CREATE INDEX idx_podcast_feedback_target ON podcast_feedback(target_artifact_id, created_at);

-- index idx_podcast_images on podcast_images
CREATE INDEX idx_podcast_images ON podcast_images(conversation_id, created_at);

-- index idx_podcast_session on podcast_conversations
CREATE INDEX idx_podcast_session ON podcast_conversations(session, created_at);

-- index idx_podcast_source on podcast_conversations
CREATE INDEX idx_podcast_source ON podcast_conversations(source_artifact_id, created_at);

-- index idx_prompt_admissions_history on prompt_admissions
CREATE INDEX idx_prompt_admissions_history ON prompt_admissions(agent_id, cooperative_principal, observed_at DESC, admission_id);

-- index idx_provider_limit_episode_open on provider_limit_episodes
CREATE INDEX idx_provider_limit_episode_open ON provider_limit_episodes(provider_instance_id, auth_generation_id, window_id, status);

-- index idx_provider_limit_events_instance on provider_limit_events
CREATE INDEX idx_provider_limit_events_instance ON provider_limit_events(provider_instance_id, observed_at DESC);

-- index idx_provider_task_turns_token on provider_task_turns
CREATE INDEX idx_provider_task_turns_token ON provider_task_turns(turn_token);

-- index idx_provider_turns_open on provider_turns
CREATE INDEX idx_provider_turns_open ON provider_turns(started_at) WHERE exited_at IS NULL;

-- index idx_provider_usage_current on provider_usage_windows
CREATE INDEX idx_provider_usage_current ON provider_usage_windows(provider_instance_id, window_kind, observed_at DESC);

-- index idx_queued_turns_agent_time on queued_turns
CREATE INDEX idx_queued_turns_agent_time ON queued_turns(agent_id, enqueued_at);

-- index idx_queued_turns_status_seq on queued_turns
CREATE INDEX idx_queued_turns_status_seq ON queued_turns(status, queue_seq);

-- index idx_runtimes_live_backend_session on runtimes
CREATE INDEX idx_runtimes_live_backend_session ON runtimes(backend_session_id) WHERE ended_at IS NULL;

-- index idx_runtimes_live_backend_session_unique on runtimes
CREATE UNIQUE INDEX idx_runtimes_live_backend_session_unique ON runtimes(backend_session_id) WHERE ended_at IS NULL AND backend_session_id IS NOT NULL AND backend_session_id != '';

-- index idx_runtimes_live_session on runtimes
CREATE INDEX idx_runtimes_live_session ON runtimes(session) WHERE ended_at IS NULL;

-- index idx_sse_events_event_id on sse_events
CREATE INDEX idx_sse_events_event_id ON sse_events(event_id);

-- index idx_sse_events_ts on sse_events
CREATE INDEX idx_sse_events_ts ON sse_events(ts);

-- index idx_state_log_agent_state on state_log
CREATE INDEX idx_state_log_agent_state ON state_log(agent_id, state_id);

-- index idx_state_log_agent_ts on state_log
CREATE INDEX idx_state_log_agent_ts ON state_log(agent_id, ts DESC);

-- index idx_task_items_plan_position on task_items
CREATE INDEX idx_task_items_plan_position ON task_items(plan_id, parent_id, position);

-- index idx_task_plans_agent_updated on task_plans
CREATE INDEX idx_task_plans_agent_updated ON task_plans(agent_id, updated_at DESC);

-- index idx_task_plans_recovery on task_plans
CREATE INDEX idx_task_plans_recovery ON task_plans(recovery_enabled, status, updated_at);

-- index idx_team_inbox_agent_status on team_inbox
CREATE INDEX idx_team_inbox_agent_status ON team_inbox(agent_id, status, team_message_id);

-- index idx_team_members_agent on team_members
CREATE INDEX idx_team_members_agent ON team_members(agent_id, team_id);

-- index idx_team_messages_team_created on team_messages
CREATE INDEX idx_team_messages_team_created ON team_messages(team_id, created_at DESC);

-- index idx_teams_active on teams
CREATE INDEX idx_teams_active ON teams(archived_at, updated_at DESC);

-- index idx_transcription_results_created on transcription_results
CREATE INDEX idx_transcription_results_created ON transcription_results(created_at);

-- index idx_tts_queue_pending on tts_queue
CREATE INDEX idx_tts_queue_pending ON tts_queue(enqueued_at) WHERE status = 'queued';

-- index idx_tts_queue_recent on tts_queue
CREATE INDEX idx_tts_queue_recent ON tts_queue(enqueued_at);

-- index idx_turn_usage_backend_at on turn_usage
CREATE INDEX idx_turn_usage_backend_at ON turn_usage(backend, at);

-- index idx_turns_agent_ts on turns
CREATE INDEX idx_turns_agent_ts ON turns(agent_id, started_at);

-- index idx_user_notifications_agent_done on user_notifications
CREATE INDEX idx_user_notifications_agent_done ON user_notifications(agent_id, done_ts DESC);

-- index idx_user_notifications_session_created on user_notifications
CREATE INDEX idx_user_notifications_session_created ON user_notifications(session, created_at DESC);

-- index idx_user_value_facts_category on user_value_facts
CREATE INDEX idx_user_value_facts_category ON user_value_facts(category, updated_at DESC);

-- index idx_user_value_facts_status on user_value_facts
CREATE INDEX idx_user_value_facts_status ON user_value_facts(status, updated_at DESC);

-- index idx_voice_events_session_ts on voice_events
CREATE INDEX idx_voice_events_session_ts ON voice_events(session, ts);

-- index idx_voice_events_trace on voice_events
CREATE INDEX idx_voice_events_trace ON voice_events(trace_id, ts);

-- index idx_voice_events_ts on voice_events
CREATE INDEX idx_voice_events_ts ON voice_events(ts);

-- index idx_voice_events_utterance on voice_events
CREATE INDEX idx_voice_events_utterance ON voice_events(utterance_id, ts);

-- index oracle_context_notifications_owner on oracle_context_notifications
CREATE INDEX oracle_context_notifications_owner ON oracle_context_notifications(owner_principal, thread_id, sent_at);

-- index oracle_threads_owner on oracle_threads
CREATE INDEX oracle_threads_owner ON oracle_threads(owner_principal, contact, updated_at);

-- index sqlite_autoindex_agent_goals_1 on agent_goals
(implicit);

-- index sqlite_autoindex_agent_portraits_1 on agent_portraits
(implicit);

-- index sqlite_autoindex_agent_schedules_1 on agent_schedules
(implicit);

-- index sqlite_autoindex_agents_1 on agents
(implicit);

-- index sqlite_autoindex_agents_2 on agents
(implicit);

-- index sqlite_autoindex_agy_turn_authority_1 on agy_turn_authority
(implicit);

-- index sqlite_autoindex_artifact_decisions_1 on artifact_decisions
(implicit);

-- index sqlite_autoindex_artifact_decisions_2 on artifact_decisions
(implicit);

-- index sqlite_autoindex_artifacts_1 on artifacts
(implicit);

-- index sqlite_autoindex_audio_bookkeeping_events_1 on audio_bookkeeping_events
(implicit);

-- index sqlite_autoindex_backend_usage_1 on backend_usage
(implicit);

-- index sqlite_autoindex_background_jobs_1 on background_jobs
(implicit);

-- index sqlite_autoindex_client_locations_1 on client_locations
(implicit);

-- index sqlite_autoindex_clips_1 on clips
(implicit);

-- index sqlite_autoindex_conversation_heads_1 on conversation_heads
(implicit);

-- index sqlite_autoindex_cursor_positions_1 on cursor_positions
(implicit);

-- index sqlite_autoindex_decision_applications_1 on decision_applications
(implicit);

-- index sqlite_autoindex_decision_deliveries_1 on decision_deliveries
(implicit);

-- index sqlite_autoindex_decisions_1 on decisions
(implicit);

-- index sqlite_autoindex_device_tokens_1 on device_tokens
(implicit);

-- index sqlite_autoindex_dream_rounds_1 on dream_rounds
(implicit);

-- index sqlite_autoindex_dream_rounds_2 on dream_rounds
(implicit);

-- index sqlite_autoindex_dream_runs_1 on dream_runs
(implicit);

-- index sqlite_autoindex_dream_threads_1 on dream_threads
(implicit);

-- index sqlite_autoindex_dream_threads_2 on dream_threads
(implicit);

-- index sqlite_autoindex_form_submissions_1 on form_submissions
(implicit);

-- index sqlite_autoindex_goal_delegations_1 on goal_delegations
(implicit);

-- index sqlite_autoindex_goal_events_1 on goal_events
(implicit);

-- index sqlite_autoindex_goal_runs_1 on goal_runs
(implicit);

-- index sqlite_autoindex_goal_subgoals_1 on goal_subgoals
(implicit);

-- index sqlite_autoindex_goals_1 on goals
(implicit);

-- index sqlite_autoindex_heartbeat_accounting_1 on heartbeat_accounting
(implicit);

-- index sqlite_autoindex_heartbeat_state_1 on heartbeat_state
(implicit);

-- index sqlite_autoindex_html_form_event_config_1 on html_form_event_config
(implicit);

-- index sqlite_autoindex_html_form_events_1 on html_form_events
(implicit);

-- index sqlite_autoindex_html_report_revisions_1 on html_report_revisions
(implicit);

-- index sqlite_autoindex_janitor_attachments_1 on janitor_attachments
(implicit);

-- index sqlite_autoindex_janitor_builtins_1 on janitor_builtins
(implicit);

-- index sqlite_autoindex_janitor_builtins_2 on janitor_builtins
(implicit);

-- index sqlite_autoindex_janitor_configs_1 on janitor_configs
(implicit);

-- index sqlite_autoindex_janitor_continuity_1 on janitor_continuity
(implicit);

-- index sqlite_autoindex_janitor_creation_requests_1 on janitor_creation_requests
(implicit);

-- index sqlite_autoindex_janitor_creation_requests_2 on janitor_creation_requests
(implicit);

-- index sqlite_autoindex_janitor_demand_claims_1 on janitor_demand_claims
(implicit);

-- index sqlite_autoindex_janitor_demand_results_1 on janitor_demand_results
(implicit);

-- index sqlite_autoindex_janitor_effects_1 on janitor_effects
(implicit);

-- index sqlite_autoindex_janitor_label_ownership_1 on janitor_label_ownership
(implicit);

-- index sqlite_autoindex_janitor_pilot_imports_1 on janitor_pilot_imports
(implicit);

-- index sqlite_autoindex_janitor_progress_1 on janitor_progress
(implicit);

-- index sqlite_autoindex_janitor_quota_receipts_1 on janitor_quota_receipts
(implicit);

-- index sqlite_autoindex_janitor_runs_1 on janitor_runs
(implicit);

-- index sqlite_autoindex_janitor_runs_2 on janitor_runs
(implicit);

-- index sqlite_autoindex_janitor_trigger_definitions_1 on janitor_trigger_definitions
(implicit);

-- index sqlite_autoindex_media_assets_1 on media_assets
(implicit);

-- index sqlite_autoindex_messages_1 on messages
(implicit);

-- index sqlite_autoindex_messages_2 on messages
(implicit);

-- index sqlite_autoindex_model_fallback_attempts_1 on model_fallback_attempts
(implicit);

-- index sqlite_autoindex_oracle_admissions_1 on oracle_admissions
(implicit);

-- index sqlite_autoindex_oracle_context_notifications_1 on oracle_context_notifications
(implicit);

-- index sqlite_autoindex_oracle_delegations_1 on oracle_delegations
(implicit);

-- index sqlite_autoindex_oracle_delegations_2 on oracle_delegations
(implicit);

-- index sqlite_autoindex_oracle_delegations_3 on oracle_delegations
(implicit);

-- index sqlite_autoindex_oracle_observations_1 on oracle_observations
(implicit);

-- index sqlite_autoindex_oracle_thread_work_1 on oracle_thread_work
(implicit);

-- index sqlite_autoindex_oracle_threads_1 on oracle_threads
(implicit);

-- index sqlite_autoindex_oracle_user_context_1 on oracle_user_context
(implicit);

-- index sqlite_autoindex_orchestrator_pending_utterances_1 on orchestrator_pending_utterances
(implicit);

-- index sqlite_autoindex_orchestrator_phrase_cache_1 on orchestrator_phrase_cache
(implicit);

-- index sqlite_autoindex_paired_devices_1 on paired_devices
(implicit);

-- index sqlite_autoindex_paired_devices_2 on paired_devices
(implicit);

-- index sqlite_autoindex_pairing_codes_1 on pairing_codes
(implicit);

-- index sqlite_autoindex_path_usage_1 on path_usage
(implicit);

-- index sqlite_autoindex_personas_1 on personas
(implicit);

-- index sqlite_autoindex_podcast_conversations_1 on podcast_conversations
(implicit);

-- index sqlite_autoindex_podcast_feedback_1 on podcast_feedback
(implicit);

-- index sqlite_autoindex_podcast_images_1 on podcast_images
(implicit);

-- index sqlite_autoindex_podcast_snapshots_1 on podcast_snapshots
(implicit);

-- index sqlite_autoindex_prompt_admissions_1 on prompt_admissions
(implicit);

-- index sqlite_autoindex_prompt_admissions_2 on prompt_admissions
(implicit);

-- index sqlite_autoindex_provider_job_cursors_1 on provider_job_cursors
(implicit);

-- index sqlite_autoindex_provider_limit_episodes_1 on provider_limit_episodes
(implicit);

-- index sqlite_autoindex_provider_limit_events_1 on provider_limit_events
(implicit);

-- index sqlite_autoindex_provider_limit_events_2 on provider_limit_events
(implicit);

-- index sqlite_autoindex_provider_task_turns_1 on provider_task_turns
(implicit);

-- index sqlite_autoindex_provider_turns_1 on provider_turns
(implicit);

-- index sqlite_autoindex_provider_usage_windows_1 on provider_usage_windows
(implicit);

-- index sqlite_autoindex_queue_state_revisions_1 on queue_state_revisions
(implicit);

-- index sqlite_autoindex_queued_turns_1 on queued_turns
(implicit);

-- index sqlite_autoindex_settings_1 on settings
(implicit);

-- index sqlite_autoindex_task_goal_documents_1 on task_goal_documents
(implicit);

-- index sqlite_autoindex_task_items_1 on task_items
(implicit);

-- index sqlite_autoindex_task_plans_1 on task_plans
(implicit);

-- index sqlite_autoindex_team_inbox_1 on team_inbox
(implicit);

-- index sqlite_autoindex_team_members_1 on team_members
(implicit);

-- index sqlite_autoindex_team_messages_1 on team_messages
(implicit);

-- index sqlite_autoindex_team_messages_2 on team_messages
(implicit);

-- index sqlite_autoindex_teams_1 on teams
(implicit);

-- index sqlite_autoindex_tool_explanation_cache_1 on tool_explanation_cache
(implicit);

-- index sqlite_autoindex_tool_explanation_demands_1 on tool_explanation_demands
(implicit);

-- index sqlite_autoindex_tool_explanation_jobs_1 on tool_explanation_jobs
(implicit);

-- index sqlite_autoindex_tool_explanation_learned_1 on tool_explanation_learned
(implicit);

-- index sqlite_autoindex_tool_explanation_mappings_1 on tool_explanation_mappings
(implicit);

-- index sqlite_autoindex_tool_explanation_releases_1 on tool_explanation_releases
(implicit);

-- index sqlite_autoindex_traces_1 on traces
(implicit);

-- index sqlite_autoindex_transcript_import_checkpoints_1 on transcript_import_checkpoints
(implicit);

-- index sqlite_autoindex_transcript_import_frontiers_1 on transcript_import_frontiers
(implicit);

-- index sqlite_autoindex_transcription_results_1 on transcription_results
(implicit);

-- index sqlite_autoindex_user_notifications_1 on user_notifications
(implicit);

-- index sqlite_autoindex_user_notifications_2 on user_notifications
(implicit);

-- index sqlite_autoindex_user_value_facts_1 on user_value_facts
(implicit);

-- index sqlite_autoindex_vocab_assignments_1 on vocab_assignments
(implicit);

-- index sqlite_autoindex_vocab_packs_1 on vocab_packs
(implicit);

-- index sqlite_autoindex_vocab_profile_packs_1 on vocab_profile_packs
(implicit);

-- index sqlite_autoindex_vocab_profiles_1 on vocab_profiles
(implicit);

-- index tool_explanation_cache_expiry on tool_explanation_cache
CREATE INDEX tool_explanation_cache_expiry ON tool_explanation_cache(expires_at);

-- index tool_explanation_cache_signature on tool_explanation_cache
CREATE INDEX tool_explanation_cache_signature ON tool_explanation_cache(signature);

-- index tool_explanation_decisions_at on tool_explanation_decisions
CREATE INDEX tool_explanation_decisions_at ON tool_explanation_decisions(at);

-- index tool_explanation_demands_owner on tool_explanation_demands
CREATE INDEX tool_explanation_demands_owner ON tool_explanation_demands(demand_id);

-- index tool_explanation_jobs_queue on tool_explanation_jobs
CREATE INDEX tool_explanation_jobs_queue ON tool_explanation_jobs(status, available_at, created_at);

-- index tool_explanation_learned_program on tool_explanation_learned
CREATE INDEX tool_explanation_learned_program ON tool_explanation_learned(program, level);

-- index tool_explanation_mappings_status on tool_explanation_mappings
CREATE INDEX tool_explanation_mappings_status ON tool_explanation_mappings(status, library_version);

-- index tool_explanation_releases_expiry on tool_explanation_releases
CREATE INDEX tool_explanation_releases_expiry ON tool_explanation_releases(expires_at);

-- index vocab_assignments_agent on vocab_assignments
CREATE UNIQUE INDEX vocab_assignments_agent ON vocab_assignments(agent_id) WHERE agent_id IS NOT NULL;

-- index vocab_assignments_team on vocab_assignments
CREATE UNIQUE INDEX vocab_assignments_team ON vocab_assignments(team_id) WHERE team_id IS NOT NULL;

-- index vocab_packs_name on vocab_packs
CREATE UNIQUE INDEX vocab_packs_name ON vocab_packs(name);

-- index vocab_profiles_name on vocab_profiles
CREATE UNIQUE INDEX vocab_profiles_name ON vocab_profiles(name);

-- index vocab_runs_recent on vocab_runs
CREATE INDEX vocab_runs_recent ON vocab_runs(created_at DESC);

-- index vocab_runs_trace on vocab_runs
CREATE INDEX vocab_runs_trace ON vocab_runs(trace_id) WHERE trace_id <> '';

-- index vocab_terms_pack_text on vocab_terms
CREATE UNIQUE INDEX vocab_terms_pack_text ON vocab_terms(pack_id, text COLLATE NOCASE);

-- table agent_goals on agent_goals
CREATE TABLE agent_goals (
    agent_id TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    backend TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    token_budget INTEGER,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    time_used_seconds INTEGER NOT NULL DEFAULT 0,
    native INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- table agent_portraits on agent_portraits
CREATE TABLE agent_portraits (
    portrait_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    media_asset_id TEXT REFERENCES media_assets(asset_id),
    storage_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    is_primary INTEGER NOT NULL DEFAULT 0,
    deleted_at INTEGER
);

-- table agent_routing_messages on agent_routing_messages
CREATE TABLE agent_routing_messages (
    routing_message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    session TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    trace_id TEXT,
    source TEXT NOT NULL DEFAULT 'orchestrator',
    created_at INTEGER NOT NULL
);

-- table agent_schedules on agent_schedules
CREATE TABLE agent_schedules (
    schedule_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session TEXT NOT NULL,
    name TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    prompt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at INTEGER,
    next_run_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- table agents on agents
CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    persona TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    session TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    deleted_at INTEGER,
    backend TEXT NOT NULL DEFAULT 'claude',
    model TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT '',
    mcp_servers TEXT NOT NULL DEFAULT '[]',
    heartbeat_enabled INTEGER NOT NULL DEFAULT 0,
    dreaming_enabled INTEGER NOT NULL DEFAULT 0,
    dreaming_last_local_date TEXT,
    muted INTEGER NOT NULL DEFAULT 0,
    custom_status TEXT NOT NULL DEFAULT '',
    avatar_symbol TEXT NOT NULL DEFAULT '',
    personality TEXT NOT NULL DEFAULT '',
    avatar_path TEXT NOT NULL DEFAULT '',
    archived_at INTEGER,
    is_janitor INTEGER NOT NULL DEFAULT 0 CHECK (is_janitor IN (0, 1)),
    parent_agent_id TEXT,
    role TEXT NOT NULL DEFAULT 'agent',
    helper_state TEXT,
    helper_completed_at INTEGER,
    custom_status_at INTEGER,
    voice_verbosity INTEGER NOT NULL DEFAULT 0
);

-- table agy_turn_authority on agy_turn_authority
CREATE TABLE agy_turn_authority (
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    backend_session_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    assistant_start_ordinal INTEGER NOT NULL,
    assistant_end_ordinal INTEGER,
    terminal_status TEXT NOT NULL DEFAULT 'pending',
    authoritative_message_id TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(agent_id, backend_session_id, trace_id)
);

-- table artifact_decisions on artifact_decisions
CREATE TABLE artifact_decisions (
    decision_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(artifact_id),
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    yes_label TEXT NOT NULL DEFAULT 'Yes',
    no_label TEXT NOT NULL DEFAULT 'No',
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_choice TEXT NOT NULL DEFAULT '',
    resolved_at INTEGER,
    resolved_by TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    expires_at INTEGER,
    response_type TEXT NOT NULL DEFAULT 'approval',
    options_json TEXT NOT NULL DEFAULT '[]',
    allow_custom_text INTEGER NOT NULL DEFAULT 0,
    recommended_option_id TEXT,
    blocks_progress INTEGER NOT NULL DEFAULT 0,
    priority_reason TEXT NOT NULL DEFAULT '',
    urgency TEXT NOT NULL DEFAULT 'normal',
    response_effort TEXT NOT NULL DEFAULT 'review',
    deadline_at INTEGER,
    answer_json TEXT
);

-- table artifacts on artifacts
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ready',
    reference_id TEXT NOT NULL DEFAULT '',
    payload_version INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL DEFAULT '{}',
    source_message_id TEXT NOT NULL DEFAULT '',
    source_trace_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER,
    deleted_at INTEGER,
    archived_at INTEGER,
    pinned_at INTEGER
);

-- table audio_bookkeeping_events on audio_bookkeeping_events
CREATE TABLE audio_bookkeeping_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    turn_id INTEGER,
    runtime_id INTEGER,
    trace_id TEXT,
    stage TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    run_id TEXT,
    completed_at INTEGER,
    UNIQUE(clip_id, stage)
);

-- table backend_usage on backend_usage
CREATE TABLE backend_usage (
    backend TEXT PRIMARY KEY,
    used_percentage REAL,
    resets_at TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    raw TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);

-- table background_job_events on background_job_events
CREATE TABLE background_job_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);

-- table background_jobs on background_jobs
CREATE TABLE background_jobs (
    job_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    cancelled_at INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    heartbeat_at INTEGER,
    heartbeat_timeout_ms INTEGER NOT NULL DEFAULT 600000,
    heartbeat_source TEXT NOT NULL DEFAULT 'unknown',
    worker_pid INTEGER,
    worker_start_token TEXT NOT NULL DEFAULT '',
    terminal_at INTEGER,
    terminal_reason TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 1,
    owner_kind TEXT NOT NULL DEFAULT 'agent',
    computer_id TEXT,
    progress_text TEXT NOT NULL DEFAULT '',
    progress_at INTEGER,
    log_path TEXT NOT NULL DEFAULT '',
    worker_cwd TEXT NOT NULL DEFAULT '',
    started_trace_id TEXT NOT NULL DEFAULT ''
);

-- table client_locations on client_locations
CREATE TABLE client_locations (
    session TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    accuracy REAL,
    ts INTEGER NOT NULL
);

-- table clips on clips
CREATE TABLE clips (
    clip_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    runtime_id INTEGER,
    turn_id INTEGER,
    path TEXT NOT NULL UNIQUE,
    voice_id TEXT,
    bytes INTEGER,
    trace_id TEXT,
    created_at INTEGER NOT NULL,
    status TEXT,
    broadcast_at INTEGER,
    queued_at INTEGER,
    play_started_at INTEGER,
    played_at INTEGER,
    error TEXT,
    producer_status TEXT,
    completed_at INTEGER
);

-- table conversation_heads on conversation_heads
CREATE TABLE conversation_heads (
    agent_id TEXT NOT NULL,
    backend_session_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    replace_revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_id, backend_session_id)
);

-- table cursor_positions on cursor_positions
CREATE TABLE cursor_positions (
    backend_session_id TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    spoken_first INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

-- table decision_applications on decision_applications
CREATE TABLE decision_applications (
    application_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    task_id TEXT,
    run_id TEXT,
    trace_id TEXT,
    applied_context TEXT NOT NULL DEFAULT '{}',
    outcome TEXT NOT NULL DEFAULT 'used',
    reason TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);

-- table decision_deliveries on decision_deliveries
CREATE TABLE decision_deliveries (
    decision_id TEXT PRIMARY KEY REFERENCES artifact_decisions(decision_id),
    artifact_id TEXT NOT NULL,
    session TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    reference_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    choice TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    delivered_at INTEGER,
    response_type TEXT NOT NULL DEFAULT 'approval',
    answer_json TEXT
);

-- table decisions on decisions
CREATE TABLE decisions (
    id TEXT PRIMARY KEY,
    canonical_question TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '{}',
    user_answer TEXT NOT NULL,
    normalized_answer TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '{}',
    tags TEXT NOT NULL DEFAULT '[]',
    risk_class TEXT NOT NULL DEFAULT 'low',
    time_horizon TEXT NOT NULL DEFAULT 'until_changed',
    confidence REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'active',
    supersedes_id TEXT,
    source_trace TEXT,
    source_message_id TEXT,
    source_agent_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    expires_at INTEGER,
    FOREIGN KEY (supersedes_id) REFERENCES decisions(id)
);

-- table device_tokens on device_tokens
CREATE TABLE device_tokens (
    token TEXT PRIMARY KEY,
    session TEXT,
    platform TEXT NOT NULL DEFAULT 'ios',
    environment TEXT NOT NULL DEFAULT 'production',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_push_at INTEGER,
    disabled_at INTEGER,
    base_url TEXT NOT NULL DEFAULT '',
    push_grant TEXT NOT NULL DEFAULT '',
    supported_conversation_modes TEXT NOT NULL DEFAULT '',
    principal TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'apns'
);

-- table dream_rounds on dream_rounds
CREATE TABLE dream_rounds (
    round_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    thread_id TEXT,
    round_index INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    prompt TEXT NOT NULL,
    response TEXT,
    target_tokens INTEGER NOT NULL,
    sent_at INTEGER,
    completed_at INTEGER,
    output_chars INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(run_id, round_index),
    FOREIGN KEY (run_id) REFERENCES dream_runs(run_id),
    FOREIGN KEY (thread_id) REFERENCES dream_threads(thread_id)
);

-- table dream_runs on dream_runs
CREATE TABLE dream_runs (
    run_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session TEXT NOT NULL,
    local_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    timezone_source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    stage TEXT NOT NULL DEFAULT 'seed',
    min_directions INTEGER NOT NULL,
    planned_directions INTEGER NOT NULL,
    planned_rounds INTEGER NOT NULL,
    completed_rounds INTEGER NOT NULL DEFAULT 0,
    target_tokens INTEGER NOT NULL,
    target_minutes INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    finished_at INTEGER,
    last_error TEXT,
    seed_strategy TEXT NOT NULL DEFAULT 'control',
    context_dose TEXT NOT NULL DEFAULT 'full',
    seed_material TEXT NOT NULL DEFAULT '',
    artifact_branch TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
);

-- table dream_threads on dream_threads
CREATE TABLE dream_threads (
    thread_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    thread_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    selected_for_iterate INTEGER NOT NULL DEFAULT 0,
    fanout_chars INTEGER NOT NULL DEFAULT 0,
    iterate_chars INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    evidence_status TEXT NOT NULL DEFAULT 'speculative',
    altitude TEXT NOT NULL DEFAULT 'idea',
    artifact_ref TEXT NOT NULL DEFAULT '',
    evidence_summary TEXT NOT NULL DEFAULT '',
    guardrail_refusals TEXT NOT NULL DEFAULT '[]',
    killed_reason TEXT NOT NULL DEFAULT '',
    origin_note TEXT NOT NULL DEFAULT '',
    UNIQUE(run_id, thread_index),
    FOREIGN KEY (run_id) REFERENCES dream_runs(run_id)
);

-- table focus on focus
CREATE TABLE focus (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 0),
    agent_id TEXT,
    updated_at INTEGER NOT NULL
);

-- table form_submissions on form_submissions
CREATE TABLE form_submissions (
    submission_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    version TEXT NOT NULL,
    session TEXT NOT NULL,
    answers_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    delivered_at INTEGER,
    synthesize_audio INTEGER NOT NULL DEFAULT 0 CHECK (synthesize_audio IN (0, 1))
);

-- table goal_delegations on goal_delegations
CREATE TABLE goal_delegations (
    delegation_id TEXT PRIMARY KEY,
    principal_agent_id TEXT NOT NULL,
    delegate_agent_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'stopped')),
    token_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    created_reason TEXT NOT NULL,
    stopped_at INTEGER,
    stopped_reason TEXT NOT NULL DEFAULT '',
    message_through INTEGER NOT NULL DEFAULT 0,
    goal_event_through INTEGER NOT NULL DEFAULT 0,
    dispatched_message_through INTEGER NOT NULL DEFAULT 0,
    dispatched_goal_event_through INTEGER NOT NULL DEFAULT 0,
    last_wake_id TEXT NOT NULL DEFAULT '',
    last_wake_at INTEGER,
    last_applied_at INTEGER,
    last_source_at INTEGER,
    last_heartbeat_at INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    job_handle TEXT NOT NULL DEFAULT '',
    unapplied_since INTEGER
);

-- table goal_events on goal_events
CREATE TABLE goal_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    at INTEGER NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT 'goal',
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('owner', 'user', 'delegate', 'system', 'peer', 'legacy', 'unattributed')),
    actor_agent_id TEXT NOT NULL DEFAULT '',
    delegation_id TEXT NOT NULL DEFAULT '',
    basis TEXT NOT NULL DEFAULT '' CHECK (basis IN ('', 'observed', 'claimed', 'inferred')),
    source_refs TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT '',
    prior_json TEXT,
    new_json TEXT,
    plan_revision INTEGER,
    idempotency_key TEXT NOT NULL,
    UNIQUE (plan_id, idempotency_key)
);

-- table goal_runs on goal_runs
CREATE TABLE goal_runs (
    run_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    evidence TEXT NOT NULL DEFAULT '{}',
    started_at INTEGER,
    finished_at INTEGER,
    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
);

-- table goal_subgoals on goal_subgoals
CREATE TABLE goal_subgoals (
    plan_id TEXT NOT NULL,
    subgoal_id TEXT NOT NULL,
    title TEXT NOT NULL,
    intent TEXT NOT NULL DEFAULT '',
    criteria_json TEXT NOT NULL DEFAULT '[]',
    owner TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('proposed', 'active', 'blocked', 'done', 'retired', 'unknown')),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    current_action TEXT NOT NULL DEFAULT '',
    next_dependency TEXT NOT NULL DEFAULT '',
    last_observed_at INTEGER,
    revision INTEGER NOT NULL DEFAULT 1,
    created_actor_kind TEXT NOT NULL,
    created_agent_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (plan_id, subgoal_id)
);

-- table goals on goals
CREATE TABLE goals (
    goal_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    source_decision_ids TEXT NOT NULL DEFAULT '[]',
    scope TEXT NOT NULL DEFAULT '{}',
    tags TEXT NOT NULL DEFAULT '[]',
    success_criteria TEXT NOT NULL DEFAULT '[]',
    risk_guardrails TEXT NOT NULL DEFAULT '[]',
    time_budget TEXT,
    owner_agent_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    satisfied_at INTEGER
);

-- table heartbeat_accounting on heartbeat_accounting
CREATE TABLE heartbeat_accounting (
    accounting_key TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    counted_at INTEGER NOT NULL
);

-- table heartbeat_state on heartbeat_state
CREATE TABLE heartbeat_state (
    agent_id TEXT PRIMARY KEY,
    last_started REAL NOT NULL DEFAULT 0,
    noop_streak INTEGER NOT NULL DEFAULT 0,
    dormant INTEGER NOT NULL DEFAULT 0,
    last_wake_signal_ms INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

-- table html_form_event_config on html_form_event_config
CREATE TABLE html_form_event_config (
    artifact_id TEXT PRIMARY KEY REFERENCES artifacts(artifact_id),
    draft_key TEXT
);

-- table html_form_events on html_form_events
CREATE TABLE html_form_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    version TEXT NOT NULL,
    event_id TEXT NOT NULL,
    client_seq INTEGER NOT NULL,
    client_at INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    UNIQUE(artifact_id, event_id)
);

-- table html_report_revisions on html_report_revisions
CREATE TABLE html_report_revisions (
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(artifact_id, version)
);

-- table janitor_attachments on janitor_attachments
CREATE TABLE janitor_attachments (
    attachment_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES janitor_configs(agent_id),
    trigger_id TEXT NOT NULL,
    trigger_version INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    next_run_at INTEGER,
    retired_at INTEGER,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(trigger_id, trigger_version) REFERENCES janitor_trigger_definitions(trigger_id, version)
);

-- table janitor_builtins on janitor_builtins
CREATE TABLE janitor_builtins (
    role TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL UNIQUE REFERENCES agents(agent_id),
    seed_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

-- table janitor_configs on janitor_configs
CREATE TABLE janitor_configs (
    agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id),
    template_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    generation INTEGER NOT NULL DEFAULT 1,
    scope_json TEXT NOT NULL DEFAULT '{}',
    execution_json TEXT NOT NULL DEFAULT '{}',
    options_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT NOT NULL DEFAULT '',
    last_run_at INTEGER,
    last_change_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- table janitor_continuity on janitor_continuity
CREATE TABLE janitor_continuity (
    target_id TEXT PRIMARY KEY,
    snapshot TEXT NOT NULL,
    run_id TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    due_at INTEGER NOT NULL,
    status TEXT NOT NULL
);

-- table janitor_creation_requests on janitor_creation_requests
CREATE TABLE janitor_creation_requests (
    request_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    session TEXT NOT NULL UNIQUE,
    agent_id TEXT REFERENCES agents(agent_id),
    response_json TEXT,
    created_at INTEGER NOT NULL,
    completed_at INTEGER
);

-- table janitor_demand_claims on janitor_demand_claims
CREATE TABLE janitor_demand_claims (
    run_id TEXT PRIMARY KEY REFERENCES janitor_runs(run_id),
    claimed_at INTEGER NOT NULL,
    finished_at INTEGER
);

-- table janitor_demand_results on janitor_demand_results
CREATE TABLE janitor_demand_results (
    run_id TEXT PRIMARY KEY REFERENCES janitor_runs(run_id),
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

-- table janitor_effects on janitor_effects
CREATE TABLE janitor_effects (
    run_id TEXT NOT NULL REFERENCES janitor_runs(run_id),
    target_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    target_session TEXT NOT NULL,
    observed_state_id INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    before_label TEXT NOT NULL,
    after_label TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    PRIMARY KEY(run_id, target_agent_id)
);

-- table janitor_label_ownership on janitor_label_ownership
CREATE TABLE janitor_label_ownership (
    target_agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id),
    owner_agent_id TEXT NOT NULL REFERENCES janitor_configs(agent_id),
    run_id TEXT NOT NULL REFERENCES janitor_runs(run_id),
    label TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    task_signature TEXT NOT NULL,
    valid_until INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- table janitor_pilot_imports on janitor_pilot_imports
CREATE TABLE janitor_pilot_imports (
    import_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES janitor_configs(agent_id),
    imported_at INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL
);

-- table janitor_progress on janitor_progress
CREATE TABLE janitor_progress (
    attachment_id TEXT PRIMARY KEY REFERENCES janitor_attachments(attachment_id),
    generation INTEGER NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    updated_at INTEGER NOT NULL
);

-- table janitor_quota_receipts on janitor_quota_receipts
CREATE TABLE janitor_quota_receipts (
    receipt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    delivery_json TEXT,
    created_at INTEGER NOT NULL
);

-- table janitor_runs on janitor_runs
CREATE TABLE janitor_runs (
    run_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES janitor_configs(agent_id),
    session TEXT NOT NULL,
    attachment_id TEXT NOT NULL REFERENCES janitor_attachments(attachment_id),
    generation INTEGER NOT NULL,
    trace_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'queued',
    outcome TEXT NOT NULL DEFAULT '',
    candidates_json TEXT NOT NULL,
    configuration_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    error TEXT NOT NULL DEFAULT '',
    activity TEXT NOT NULL DEFAULT '',
    activity_summary TEXT NOT NULL DEFAULT ''
);

-- table janitor_trigger_definitions on janitor_trigger_definitions
CREATE TABLE janitor_trigger_definitions (
    trigger_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    defaults_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (trigger_id, version)
);

-- table judgment_decisions on judgment_decisions
CREATE TABLE judgment_decisions (
    judgment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    trace_id TEXT,
    question_ids TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    answers_json TEXT NOT NULL DEFAULT '{}',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at INTEGER NOT NULL
);

-- table media_assets on media_assets
CREATE TABLE media_assets (
    asset_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session TEXT NOT NULL,
    source_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    storage_path TEXT NOT NULL,
    caption TEXT,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    deleted_at INTEGER
);

-- table message_clock on message_clock
CREATE TABLE message_clock (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 0),
    revision INTEGER NOT NULL
);

-- table messages on messages
CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    backend_session_id TEXT,
    source_file TEXT,
    seq INTEGER NOT NULL,
    role TEXT,
    timestamp TEXT,
    text TEXT NOT NULL,
    kind TEXT,
    tool_name TEXT,
    tools_json TEXT NOT NULL DEFAULT '[]',
    updated_at INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    display_cells_json TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL DEFAULT 'user',
    sender_agent_id TEXT,
    prompt_admission_id TEXT,
    phase TEXT,
    turn_json TEXT,
    trace_id TEXT,
    UNIQUE(agent_id, backend_session_id, seq)
);

-- table model_fallback_attempts on model_fallback_attempts
CREATE TABLE model_fallback_attempts (
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    request_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    result_json TEXT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    runtime_id INTEGER,
    PRIMARY KEY(agent_id, request_id, attempt)
);

-- table oracle_admissions on oracle_admissions
CREATE TABLE oracle_admissions (
    thread_id TEXT NOT NULL REFERENCES oracle_threads(thread_id),
    revision INTEGER NOT NULL,
    action_index INTEGER NOT NULL,
    call_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    result_json TEXT,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(thread_id, revision, action_index)
);

-- table oracle_context_notifications on oracle_context_notifications
CREATE TABLE oracle_context_notifications (
    thread_id TEXT NOT NULL REFERENCES oracle_threads(thread_id),
    owner_principal TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    reference_ts INTEGER NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0,
    sent_at INTEGER NOT NULL,
    PRIMARY KEY(thread_id, source_kind, source_id)
);

-- table oracle_delegations on oracle_delegations
CREATE TABLE oracle_delegations (
    delegation_id TEXT PRIMARY KEY,
    owner_principal TEXT NOT NULL,
    trace_id TEXT NOT NULL UNIQUE,
    client_msg_id TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session TEXT NOT NULL,
    backend_session_id TEXT NOT NULL DEFAULT '',
    request_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted' CHECK(status IN ('accepted', 'queued', 'completed', 'failed', 'cancelled')),
    result_message_id TEXT,
    result_text TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    delivered_at INTEGER,
    completion_trace_id TEXT NOT NULL DEFAULT ''
);

-- table oracle_observations on oracle_observations
CREATE TABLE oracle_observations (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL REFERENCES oracle_threads(thread_id),
    source_key TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(thread_id, source_key)
);

-- table oracle_thread_work on oracle_thread_work
CREATE TABLE oracle_thread_work (
    thread_id TEXT NOT NULL REFERENCES oracle_threads(thread_id),
    operation_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(thread_id, operation_id)
);

-- table oracle_threads on oracle_threads
CREATE TABLE oracle_threads (
    thread_id TEXT PRIMARY KEY,
    owner_principal TEXT NOT NULL,
    contact TEXT NOT NULL,
    active_connection TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- table oracle_user_context on oracle_user_context
CREATE TABLE oracle_user_context (
    thread_id TEXT NOT NULL REFERENCES oracle_threads(thread_id),
    context_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    mime_type TEXT,
    image BLOB,
    captured_at INTEGER,
    added_at INTEGER NOT NULL,
    removed_at INTEGER,
    content_hash TEXT NOT NULL,
    PRIMARY KEY(thread_id, context_id)
);

-- table orchestrator_decisions on orchestrator_decisions
CREATE TABLE orchestrator_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT,
    utterance TEXT NOT NULL,
    requested_session TEXT,
    hands_free INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 0,
    provider TEXT,
    model TEXT,
    effort TEXT,
    latency_ms INTEGER,
    context_hash TEXT,
    context_agent_count INTEGER NOT NULL DEFAULT 0,
    context_message_count INTEGER NOT NULL DEFAULT 0,
    decision_kind TEXT NOT NULL,
    target_session TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    addressing INTEGER NOT NULL DEFAULT 0,
    mentioned_sessions_json TEXT NOT NULL DEFAULT '[]',
    name_corrections_json TEXT NOT NULL DEFAULT '[]',
    candidate_scores_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT,
    raw_response_json TEXT NOT NULL DEFAULT '{}',
    final_action TEXT NOT NULL,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    phrase_key TEXT,
    error TEXT,
    created_at INTEGER NOT NULL
);

-- table orchestrator_pending_utterances on orchestrator_pending_utterances
CREATE TABLE orchestrator_pending_utterances (
    pending_id TEXT PRIMARY KEY,
    trace_id TEXT,
    utterance TEXT NOT NULL,
    requested_session TEXT,
    candidate_session TEXT,
    speak_as_session TEXT,
    reason TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    prompt_admission_json TEXT NOT NULL DEFAULT ''
);

-- table orchestrator_phrase_cache on orchestrator_phrase_cache
CREATE TABLE orchestrator_phrase_cache (
    phrase_key TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    session TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    audio_path TEXT,
    provider TEXT,
    model TEXT,
    generated_at INTEGER,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (phrase_key, voice_id, session)
);

-- table paired_devices on paired_devices
CREATE TABLE paired_devices (
    device_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL CHECK(scope IN ('full', 'limited')),
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER,
    revoked_at INTEGER
);

-- table pairing_codes on pairing_codes
CREATE TABLE pairing_codes (
    code_hash TEXT PRIMARY KEY,
    device_name TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('full', 'limited')),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER
);

-- table path_usage on path_usage
CREATE TABLE path_usage (
    path TEXT PRIMARY KEY,
    use_count INTEGER NOT NULL DEFAULT 0,
    first_used_at INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL
);

-- table personas on personas
CREATE TABLE personas (
    persona_id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE,
    voice_id TEXT NOT NULL DEFAULT '',
    avatar_symbol TEXT NOT NULL DEFAULT '',
    avatar_path TEXT NOT NULL DEFAULT '',
    personality TEXT NOT NULL DEFAULT '',
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    deleted_at INTEGER
);

-- table podcast_conversations on podcast_conversations
CREATE TABLE podcast_conversations (
    conversation_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    session TEXT NOT NULL,
    revision TEXT NOT NULL,
    position REAL NOT NULL,
    episode_snapshot TEXT NOT NULL REFERENCES podcast_snapshots(sha256),
    source_artifact_id TEXT NOT NULL DEFAULT '',
    source_snapshot TEXT REFERENCES podcast_snapshots(sha256),
    context_json TEXT NOT NULL,
    configuration_json TEXT NOT NULL,
    model TEXT NOT NULL,
    voice TEXT NOT NULL,
    owner_instance TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    closed_at INTEGER,
    last_event_id INTEGER NOT NULL DEFAULT 0
);

-- table podcast_feedback on podcast_feedback
CREATE TABLE podcast_feedback (
    feedback_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES podcast_conversations(conversation_id),
    target_artifact_id TEXT NOT NULL,
    target_snapshot TEXT NOT NULL REFERENCES podcast_snapshots(sha256),
    through_event_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    image_ids_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    payload_hash TEXT NOT NULL
);

-- table podcast_images on podcast_images
CREATE TABLE podcast_images (
    image_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES podcast_conversations(conversation_id),
    asset_id TEXT NOT NULL REFERENCES media_assets(asset_id),
    question TEXT NOT NULL,
    question_event_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

-- table podcast_snapshots on podcast_snapshots
CREATE TABLE podcast_snapshots (
    sha256 TEXT PRIMARY KEY,
    content_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

-- table prompt_admissions on prompt_admissions
CREATE TABLE prompt_admissions (
    admission_id TEXT PRIMARY KEY,
    admission_version INTEGER NOT NULL,
    authenticated_at_admission INTEGER NOT NULL,
    cooperative_principal TEXT NOT NULL,
    principal_id TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL,
    sender_agent_id TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    client_admission_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session TEXT NOT NULL,
    message_id TEXT NOT NULL,
    original_text TEXT NOT NULL,
    client TEXT NOT NULL DEFAULT '',
    UNIQUE(agent_id, client_admission_id)
);

-- table provider_job_cursors on provider_job_cursors
CREATE TABLE provider_job_cursors (
    agent_id TEXT NOT NULL,
    native_id TEXT NOT NULL,
    path TEXT NOT NULL,
    file_identity TEXT NOT NULL,
    position INTEGER NOT NULL,
    discarding INTEGER NOT NULL DEFAULT 0,
    action_offset INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(agent_id, native_id)
);

-- table provider_limit_episodes on provider_limit_episodes
CREATE TABLE provider_limit_episodes (
    episode_id TEXT PRIMARY KEY,
    provider_instance_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    auth_generation_id TEXT NOT NULL,
    window_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    current_kind TEXT NOT NULL,
    threshold_id TEXT,
    opened_at INTEGER NOT NULL,
    resolved_at INTEGER,
    current_event_id TEXT
);

-- table provider_limit_events on provider_limit_events
CREATE TABLE provider_limit_events (
    provider_limit_event_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL REFERENCES provider_limit_episodes(episode_id),
    provider_instance_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    auth_generation_id TEXT NOT NULL,
    window_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    threshold_id TEXT,
    used_percentage REAL,
    resets_at TEXT,
    observed_at INTEGER NOT NULL,
    freshness TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE
);

-- table provider_task_turns on provider_task_turns
CREATE TABLE provider_task_turns (
    agent_id TEXT NOT NULL,
    native_id TEXT NOT NULL,
    tool_use_id TEXT NOT NULL,
    turn_token TEXT NOT NULL,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY(agent_id, native_id, tool_use_id)
);

-- table provider_turns on provider_turns
CREATE TABLE provider_turns (
    turn_token TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    pid INTEGER,
    start_token TEXT NOT NULL DEFAULT '',
    started_at INTEGER NOT NULL,
    exited_at INTEGER
);

-- table provider_usage_windows on provider_usage_windows
CREATE TABLE provider_usage_windows (
    provider_instance_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    auth_generation_id TEXT NOT NULL,
    account_scope_ref TEXT,
    window_id TEXT NOT NULL,
    window_kind TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    unit TEXT NOT NULL,
    used_percentage REAL,
    resets_at TEXT,
    observed_at INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    source_detail TEXT NOT NULL,
    PRIMARY KEY(provider_instance_id, auth_generation_id, window_id)
);

-- table queue_state_revisions on queue_state_revisions
CREATE TABLE queue_state_revisions (
    agent_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0
);

-- table queued_turns on queued_turns
CREATE TABLE queued_turns (
    queue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL,
    session TEXT NOT NULL,
    text TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    client_msg_id TEXT NOT NULL DEFAULT '',
    synthesize_audio INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL DEFAULT 'user',
    sender_agent_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    enqueued_at INTEGER NOT NULL,
    started_at INTEGER,
    claimed_at INTEGER,
    prompt_admission_id TEXT NOT NULL DEFAULT '',
    allow_paused INTEGER NOT NULL DEFAULT 0
);

-- table runtimes on runtimes
CREATE TABLE runtimes (
    runtime_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session TEXT NOT NULL,
    backend_session_id TEXT,
    started_at INTEGER NOT NULL,
    ended_at INTEGER
);

-- table settings on settings
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- table sqlite_sequence on sqlite_sequence
CREATE TABLE sqlite_sequence (
    name,
    seq
);

-- table sse_events on sse_events
CREATE TABLE sse_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    type TEXT NOT NULL,
    session TEXT,
    agent_id TEXT,
    payload TEXT NOT NULL
);

-- table state_log on state_log
CREATE TABLE state_log (
    state_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    runtime_id INTEGER,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);

-- table task_goal_documents on task_goal_documents
CREATE TABLE task_goal_documents (
    plan_id TEXT NOT NULL REFERENCES task_plans(plan_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    format TEXT NOT NULL,
    content_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(plan_id, name, revision)
);

-- table task_items on task_items
CREATE TABLE task_items (
    item_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES task_plans(plan_id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES task_items(item_id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    active_ms INTEGER NOT NULL DEFAULT 0,
    required INTEGER NOT NULL DEFAULT 1
);

-- table task_plans on task_plans
CREATE TABLE task_plans (
    plan_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    completed_at INTEGER,
    history_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 0,
    goal_json TEXT NOT NULL DEFAULT '{}',
    recovery_enabled INTEGER NOT NULL DEFAULT 0
);

-- table team_inbox on team_inbox
CREATE TABLE team_inbox (
    team_message_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unread',
    injected_at INTEGER,
    read_at INTEGER,
    PRIMARY KEY (team_message_id, agent_id)
);

-- table team_members on team_members
CREATE TABLE team_members (
    team_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (team_id, agent_id)
);

-- table team_messages on team_messages
CREATE TABLE team_messages (
    team_message_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    source_agent_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    trace_id TEXT,
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(team_id, source_message_id, text)
);

-- table teams on teams
CREATE TABLE teams (
    team_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    color TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    archived_at INTEGER,
    nudge_enabled INTEGER NOT NULL DEFAULT 1,
    leader_agent_id TEXT,
    parent_team_id TEXT,
    leader_enabled INTEGER NOT NULL DEFAULT 0,
    communication_enabled INTEGER NOT NULL DEFAULT 0
);

-- table tool_explanation_cache on tool_explanation_cache
CREATE TABLE tool_explanation_cache (
    cache_key TEXT PRIMARY KEY,
    explanation TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'llm',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    signature TEXT NOT NULL DEFAULT ''
);

-- table tool_explanation_decisions on tool_explanation_decisions
CREATE TABLE tool_explanation_decisions (
    id INTEGER PRIMARY KEY,
    at INTEGER NOT NULL,
    agent_id TEXT NOT NULL DEFAULT '',
    signature TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    parts INTEGER NOT NULL DEFAULT 1,
    jev_confidence REAL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
);

-- table tool_explanation_demands on tool_explanation_demands
CREATE TABLE tool_explanation_demands (
    cache_key TEXT NOT NULL REFERENCES tool_explanation_jobs(cache_key) ON DELETE CASCADE,
    demand_id TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY(cache_key, demand_id)
);

-- table tool_explanation_jobs on tool_explanation_jobs
CREATE TABLE tool_explanation_jobs (
    cache_key TEXT PRIMARY KEY,
    detail_level INTEGER NOT NULL,
    activity_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    available_at INTEGER NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    lease_until INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT NOT NULL DEFAULT ''
);

-- table tool_explanation_learned on tool_explanation_learned
CREATE TABLE tool_explanation_learned (
    signature TEXT NOT NULL,
    level INTEGER NOT NULL,
    template_text TEXT NOT NULL DEFAULT '',
    template_id TEXT NOT NULL DEFAULT '',
    argument_index INTEGER,
    slot_names TEXT NOT NULL DEFAULT '[]',
    program TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    producer TEXT NOT NULL,
    parameterised INTEGER NOT NULL DEFAULT 1,
    confidence REAL,
    prompt_version INTEGER NOT NULL,
    templates_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    last_hit_at INTEGER,
    hits INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    source_signature TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (signature, level)
);

-- table tool_explanation_mappings on tool_explanation_mappings
CREATE TABLE tool_explanation_mappings (
    signature TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    library_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    conflicts INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    promoted_at INTEGER
);

-- table tool_explanation_releases on tool_explanation_releases
CREATE TABLE tool_explanation_releases (
    demand_id TEXT PRIMARY KEY,
    expires_at INTEGER NOT NULL
);

-- table traces on traces
CREATE TABLE traces (
    agent_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

-- table transcript_import_checkpoints on transcript_import_checkpoints
CREATE TABLE transcript_import_checkpoints (
    agent_id TEXT NOT NULL,
    backend_session_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    backend TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    complete_size INTEGER NOT NULL,
    boundary_offset INTEGER NOT NULL,
    boundary_seq INTEGER NOT NULL,
    turn_count INTEGER NOT NULL,
    prefix_hash TEXT NOT NULL,
    tail_hash TEXT NOT NULL,
    prefix_turns_hash TEXT NOT NULL,
    parser_carry_json TEXT NOT NULL,
    store_carry_json TEXT NOT NULL,
    imports_since_audit INTEGER NOT NULL DEFAULT 0,
    audited_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (agent_id, backend_session_id, source_file)
);

-- table transcript_import_frontiers on transcript_import_frontiers
CREATE TABLE transcript_import_frontiers (
    agent_id TEXT NOT NULL,
    backend_session_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    checkpoint_digest TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    inputs_digest TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (agent_id, backend_session_id, source_file)
);

-- table transcription_results on transcription_results
CREATE TABLE transcription_results (
    job_id TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

-- table tts_queue on tts_queue
CREATE TABLE tts_queue (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    text TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    session TEXT NOT NULL,
    source TEXT NOT NULL,
    trace_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    enqueued_at INTEGER NOT NULL,
    claimed_at INTEGER,
    completed_at INTEGER,
    clip_id INTEGER
);

-- table turn_usage on turn_usage
CREATE TABLE turn_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    backend TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    trace_id TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    duration_ms INTEGER,
    at INTEGER NOT NULL
);

-- table turns on turns
CREATE TABLE turns (
    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    runtime_id INTEGER,
    source TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    synthesize_audio INTEGER NOT NULL DEFAULT 1,
    settled_at INTEGER,
    outcome TEXT
);

-- table user_notifications on user_notifications
CREATE TABLE user_notifications (
    notification_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session TEXT NOT NULL,
    persona TEXT NOT NULL,
    backend_session_id TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    done_ts INTEGER NOT NULL,
    source_message_id TEXT NOT NULL DEFAULT '',
    cause_message_id TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    notify INTEGER NOT NULL DEFAULT 0,
    push INTEGER NOT NULL DEFAULT 0,
    badge INTEGER NOT NULL DEFAULT 0,
    unread INTEGER NOT NULL DEFAULT 0,
    preview TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    muted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(agent_id, done_ts)
);

-- table user_value_facts on user_value_facts
CREATE TABLE user_value_facts (
    fact_id TEXT PRIMARY KEY,
    decision_id TEXT,
    statement TEXT NOT NULL,
    category TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '{}',
    tags TEXT NOT NULL DEFAULT '[]',
    evidence_count INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL DEFAULT 0.7,
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES decisions(id)
);

-- table vocab_assignments on vocab_assignments
CREATE TABLE vocab_assignments (
    assignment_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES vocab_profiles(profile_id) ON DELETE CASCADE,
    agent_id TEXT REFERENCES agents(agent_id) ON DELETE CASCADE,
    team_id TEXT,
    created_at INTEGER NOT NULL,
    CHECK ((agent_id IS NULL) <> (team_id IS NULL))
);

-- table vocab_packs on vocab_packs
CREATE TABLE vocab_packs (
    pack_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'static' CHECK(kind IN ('static', 'dynamic')),
    generator TEXT NOT NULL DEFAULT '',
    priority REAL NOT NULL DEFAULT 1.0,
    floor INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- table vocab_profile_packs on vocab_profile_packs
CREATE TABLE vocab_profile_packs (
    profile_id TEXT NOT NULL REFERENCES vocab_profiles(profile_id) ON DELETE CASCADE,
    pack_id TEXT NOT NULL REFERENCES vocab_packs(pack_id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (profile_id, pack_id)
);

-- table vocab_profiles on vocab_profiles
CREATE TABLE vocab_profiles (
    profile_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- table vocab_runs on vocab_runs
CREATE TABLE vocab_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT REFERENCES agents(agent_id) ON DELETE SET NULL,
    session TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    profile_id TEXT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    unit TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    used INTEGER NOT NULL,
    form TEXT NOT NULL,
    rarity_floor REAL NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '',
    included_json TEXT NOT NULL DEFAULT '[]',
    dropped_json TEXT NOT NULL DEFAULT '[]',
    transcript TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

-- table vocab_terms on vocab_terms
CREATE TABLE vocab_terms (
    term_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id TEXT NOT NULL REFERENCES vocab_packs(pack_id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    say_as TEXT NOT NULL DEFAULT '',
    often_heard_as TEXT NOT NULL DEFAULT '',
    rarity REAL NOT NULL DEFAULT 0.5,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at INTEGER NOT NULL
);

-- table voice_events on voice_events
CREATE TABLE voice_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    client_ts INTEGER,
    mono_ms INTEGER,
    received_at INTEGER NOT NULL,
    clock_offset_ms INTEGER,
    source TEXT NOT NULL,
    client_id TEXT,
    session TEXT,
    utterance_id TEXT,
    trace_id TEXT,
    event TEXT NOT NULL,
    duration_ms REAL,
    level_db REAL,
    peak_db REAL,
    text TEXT,
    detail TEXT NOT NULL DEFAULT '{}'
);

-- trigger audio_bookkeeping_client on clips
CREATE TRIGGER audio_bookkeeping_client AFTER UPDATE OF status ON clips WHEN NEW.status IS NOT OLD.status BEGIN INSERT OR IGNORE INTO audio_bookkeeping_events(clip_id, agent_id, turn_id, runtime_id, trace_id, stage, created_at) VALUES(NEW.clip_id, NEW.agent_id, NEW.turn_id, NEW.runtime_id, NEW.trace_id, 'client:'||NEW.status, CAST(strftime('%s', 'now') AS INTEGER)*1000); END;

-- trigger audio_bookkeeping_created on clips
CREATE TRIGGER audio_bookkeeping_created AFTER INSERT ON clips BEGIN INSERT OR IGNORE INTO audio_bookkeeping_events(clip_id, agent_id, turn_id, runtime_id, trace_id, stage, created_at) VALUES(NEW.clip_id, NEW.agent_id, NEW.turn_id, NEW.runtime_id, NEW.trace_id, 'producer:'||COALESCE(NEW.producer_status, 'complete'), NEW.created_at); INSERT OR IGNORE INTO audio_bookkeeping_events(clip_id, agent_id, turn_id, runtime_id, trace_id, stage, created_at) VALUES(NEW.clip_id, NEW.agent_id, NEW.turn_id, NEW.runtime_id, NEW.trace_id, 'client:'||NEW.status, NEW.created_at); END;

-- trigger audio_bookkeeping_producer on clips
CREATE TRIGGER audio_bookkeeping_producer AFTER UPDATE OF producer_status ON clips WHEN NEW.producer_status IS NOT OLD.producer_status BEGIN INSERT OR IGNORE INTO audio_bookkeeping_events(clip_id, agent_id, turn_id, runtime_id, trace_id, stage, created_at) VALUES(NEW.clip_id, NEW.agent_id, NEW.turn_id, NEW.runtime_id, NEW.trace_id, 'producer:'||NEW.producer_status, CAST(strftime('%s', 'now') AS INTEGER)*1000); END;

-- trigger goal_events_no_delete on goal_events
CREATE TRIGGER goal_events_no_delete BEFORE DELETE ON goal_events BEGIN SELECT RAISE(ABORT, 'goal events are append-only'); END;

-- trigger goal_events_no_update on goal_events
CREATE TRIGGER goal_events_no_update BEFORE UPDATE ON goal_events BEGIN SELECT RAISE(ABORT, 'goal events are append-only'); END;

-- trigger goal_subgoals_no_delete on goal_subgoals
CREATE TRIGGER goal_subgoals_no_delete BEFORE DELETE ON goal_subgoals BEGIN SELECT RAISE(ABORT, 'subgoals are retired, never deleted'); END;

-- trigger html_form_events_no_delete on html_form_events
CREATE TRIGGER html_form_events_no_delete BEFORE DELETE ON html_form_events BEGIN SELECT RAISE(ABORT, 'Form events are append-only'); END;

-- trigger html_form_events_no_update on html_form_events
CREATE TRIGGER html_form_events_no_update BEFORE UPDATE ON html_form_events BEGIN SELECT RAISE(ABORT, 'Form events are immutable'); END;

-- trigger janitor_demand_trigger_no_delete on janitor_trigger_definitions
CREATE TRIGGER janitor_demand_trigger_no_delete BEFORE DELETE ON janitor_trigger_definitions WHEN OLD.kind='demand' BEGIN SELECT RAISE(ABORT, 'Demand trigger versions are immutable'); END;

-- trigger janitor_demand_trigger_no_update on janitor_trigger_definitions
CREATE TRIGGER janitor_demand_trigger_no_update BEFORE UPDATE ON janitor_trigger_definitions WHEN OLD.kind='demand' BEGIN SELECT RAISE(ABORT, 'Demand trigger versions are immutable'); END;

-- view clip_lifecycle on clip_lifecycle
CREATE VIEW clip_lifecycle AS SELECT c.clip_id, c.trace_id, c.agent_id, c.path AS clip_url, c.status, c.producer_status, c.created_at, c.broadcast_at, c.queued_at, c.play_started_at, c.played_at, c.completed_at, c.error FROM clips c;
