<script>
  // Settings > Language: 'System' (this browser's languages), then each shipped language in its
  // own name. Applies at once; kept on this device (stores/i18n.svelte.js).
  import { i18n, setLanguage, t } from '../stores/i18n.svelte.js';
  import { ENDONYMS, LANGUAGES } from './i18n/core.js';
</script>

<label class="start-field">
  <span>{t('settings.language.title')}</span>
  <select id="settingsLanguage" value={i18n.choice} onchange={e => setLanguage(e.currentTarget.value)}>
    <option value="system">{t('settings.language.system')}</option>
    {#each LANGUAGES as lang (lang)}<option value={lang} lang={lang}>{ENDONYMS[lang]}</option>{/each}
  </select>
</label>
