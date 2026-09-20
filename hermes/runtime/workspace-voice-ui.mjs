// Expose Hermes' shared STT settings without replacing its transcription backend.
export function workspaceVoiceUi() {
  return { name: 'hermes-voice-settings', enforce: 'pre', transform(source, id) {
    const file = id.split('?')[0];
    let code = source;
    const replace = (anchor, value) => {
      if (code.split(anchor).length !== 2) throw new Error('Unsupported Workspace voice contract: ' + file);
      code = code.replace(anchor, value);
    };
    if (file.endsWith('/src/lib/stt-config.ts')) {
      replace('export const STT_PROVIDER_OPTIONS = [',
        "export const STT_PROVIDER_OPTIONS = [\n  { value: 'nous', label: 'Nous (Hermes gateway)' },");
    } else if (file.endsWith('/src/components/settings-dialog/settings-dialog.tsx')) {
      // Limit modifications to VoiceContent; leave unrelated settings alone.
      const start = code.indexOf('function VoiceContent() {');
      const end = code.indexOf('function DisplayContent()', start);
      if (start < 0 || end < 0) throw new Error('Unsupported Workspace voice contract: ' + file);
      const before = code.slice(0, start);
      const after = code.slice(end);
      code = code.slice(start, end);
      const languageRow = `            <Row label="Language" description="Optional BCP-47 code, e.g. en or en-US.">
              <Input
                value={String(stt.language || '')}
                onChange={(e) => saveStt('language', e.target.value)}
                placeholder="auto"
                className="h-8 w-40"
              />
            </Row>`;
      replace(languageRow, '');
      replace('          </>\n        )}', `          </>
        )}
        <Row label="Recognition language" description="Leave empty for automatic detection (Russian stays Russian). Use ru only for Russian-only recordings. Provider-specific language overrides take precedence; deploy restores automatic detection.">
          <Input
            defaultValue={String(stt.language || '')}
            key={String(stt.language || '')}
            onBlur={(e) => { if (e.target.value !== String(stt.language || '')) void saveStt('language', e.target.value) }}
            placeholder="auto"
            className="h-8 w-40"
          />
        </Row>
        <Row label="Speech vocabulary" description="Optional short vocabulary of names and technical terms, not translation instructions. Used by supporting Hermes STT providers for Telegram voice messages.">
          <Input
            defaultValue={String(stt.prompt || '')}
            key={String(stt.prompt || '')}
            onBlur={(e) => { if (e.target.value !== String(stt.prompt || '')) void saveStt('prompt', e.target.value) }}
            placeholder="Hermes, Tailscale, Playwright, SDET"
          />
        </Row>
        {sttProvider === 'nous' && <p className="text-xs text-primary-500">Nous recognition is handled by Hermes (including Telegram). This Workspace version does not support Nous for its own browser microphone.</p>}`);
      replace("  const saveStt = async (key: string, value: unknown) => {\n    setMsg(null)\n    try {\n      await fetch", `  const saveStt = async (key: string, value: unknown) => {
    if (key === 'language' && typeof value === 'string') {
      value = value.trim().toLowerCase() === 'auto' ? '' : value.trim()
    }
    setMsg(null)
    try {
      const response = await fetch`);
      replace("body: JSON.stringify({ config: { stt: { [key]: value } } }),\n      })",
        "body: JSON.stringify({ config: { stt: { [key]: value } } }),\n      })\n      if (!response.ok) throw new Error('Voice settings were not saved')");
      code = before + code + after;
    } else return null;
    return { code, map: null };
  } };
}
