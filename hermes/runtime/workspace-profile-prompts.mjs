// Browser-only helper. Reuse the existing authenticated/CSRF-checked editor;
// never persist a second prompt in config.yaml or silently overwrite stale text.
export async function saveProfileSoul(soulPath, content, original, fetchImpl = fetch) {
  if (typeof soulPath !== 'string' || !/^(?:profiles\/[a-z0-9][a-z0-9_-]{0,63}\/)?SOUL\.md$/.test(soulPath)) {
    throw new Error('Invalid profile SOUL path');
  }
  const response = await fetchImpl('/api/memory/read?path=' + encodeURIComponent(soulPath));
  if (!response.ok) throw new Error(`Cannot read SOUL (${response.status}); sign in or reload`);
  const current = await response.json();
  if (typeof original !== 'string' || typeof content !== 'string' ||
      current.content !== original || typeof current.version !== 'string') {
    throw new Error('SOUL changed; reload before saving and keep your draft separately');
  }
  if (content === original) return;
  const saved = await fetchImpl('/api/memory/write', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: soulPath, content, version: current.version }),
  });
  if (!saved.ok) throw new Error(`Cannot save SOUL (${saved.status}); keep your draft and reload`);
}
