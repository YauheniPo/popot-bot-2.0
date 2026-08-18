## What changed

<!-- Explain the user-visible result and why this change is needed. -->

## Privacy and API impact

- [ ] No personal data, bot token, `.env` file, raw Telegram update or precise
      coordinates were added to the repository or logs.
- [ ] If location handling changed, the disclosure before the Telegram share
      action and the Nominatim/OpenStreetMap behavior were reviewed.
- [ ] If Telegram or Nominatim calls changed, error handling and rate limiting
      were reviewed.

## Verification

- [ ] `cd telegram-user-info-bot && python -m unittest discover -s tests -v`
- [ ] I added or updated tests for changed behavior.

## Deployment impact

<!-- Mention required environment/configuration changes, or write “None”. -->
