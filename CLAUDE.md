# Telegram User Info Bot — Claude Code instructions

## Security and privacy

- Treat Telegram updates, profile data, contact details, coordinates, `.env`
  files and bot tokens as sensitive. Never print, commit or log them.
- Keep report generation limited to private chats. Do not weaken the contact
  owner check or the explicit share flow.
- Before a shared location reaches OpenStreetMap Nominatim, the UI must clearly
  disclose that precise coordinates are sent to that third party. Do not add
  local persistence of shared coordinates.
- Preserve Nominatim's rate limiting, identifying User-Agent, timeout and
  resilient error handling.

## Review standards

- Report only actionable, user-impacting findings. Do not report style nits.
- Check changes to Telegram API error handling for accidental data exposure.
- Check that tests cover modified behavior and have not been weakened merely to
  make CI pass.
- Do not approve, merge, push, or modify code while performing a review.

## Verification

Run the relevant tests from the bot directory:

```bash
python -m unittest discover -s tests -v
```
