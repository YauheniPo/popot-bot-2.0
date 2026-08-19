# Telegram User Info Bot

The bot sends a plain UTF-8 text report containing all user data available
through the Telegram Bot API when it receives `/start` or `/me`. The `/share`
command displays Telegram system buttons for voluntarily sharing a phone number
or location. For a shared location, the bot also shows the place, city, time
zone, UTC offset, and local time.

The report uses the `.txt` extension and includes a UTF-8 BOM so that Unicode
text is recognized correctly by the Telegram viewer on iPhone. It does not use
Markdown formatting.

The report includes:

- the User object from the incoming message;
- the complete raw Telegram `Update`;
- known `User` fields with `returned: true/false` availability markers;
- complete private chat information from `getChat`;
- known `ChatFullInfo` fields, including bio, birthdate, emoji status, business
  information, and rating;
- available chat member data from `getChatMember`;
- metadata for all available profile photos and audio files;
- up to 20 available messages from the user's personal channel;
- explanations for data that the Bot API does not expose automatically.

If Telegram does not support a method or does not grant access to it, the error
is written to the corresponding report section. The bot does not generate a
report in groups to avoid exposing user data.

The Telegram Bot API does not automatically expose a phone number, email
address, IP address, device information, private message history, or contact
list. A phone number or location is available only after the user explicitly
shares it through a supported Telegram flow.

A missing field can mean that it is not applicable, hidden by privacy settings,
or was not returned by Telegram. Raw responses are included without field
filtering, so new Bot API fields will also appear in the report.

## Voluntarily sharing data

1. Send `/share` to the bot in a private chat.
2. Select **Share phone number** or **Share location**.
3. Confirm the action in Telegram.
4. The bot sends a new text report. The shared value appears in the
   `Data explicitly shared by the user` and `Complete incoming Telegram Update`
   sections.

A contact is accepted only when its `user_id` matches the sender's ID, which
prevents someone else's number from being added to the report. For a location,
the Bot API cannot distinguish the current position from a place manually
selected by the user. Shared values are not saved to disk. Select
**Hide buttons** to close the keyboard.

After a location is shared, the bot:

- sends the coordinates to
  [OpenStreetMap Nominatim](https://nominatim.org/release-docs/latest/api/Reverse/)
  to obtain a readable address, city, and country;
- determines the IANA time zone locally with `timezonefinder`;
- displays the result in the chat and includes the complete Nominatim response
  in the text report.

Coordinates are sent to the external service only after the user explicitly
shares a location. The bot limits public Nominatim usage to one request per
second, follows the
[Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/),
and includes the © OpenStreetMap contributors attribution. The address language
is configured with `NOMINATIM_LANGUAGE` in `.env` and defaults to English.

## Initial setup and launch

Python 3.11 or newer is required.

1. Create a bot with [@BotFather](https://t.me/BotFather) and obtain its token.
2. From the repository root, open the project directory, create a virtual
   environment, and install the dependencies:

   ```bash
   cd telegram-user-info-bot
   python3.11 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   ```

3. Create the local configuration file:

   ```bash
   cp .env.example .env
   ```

4. Open `.env`, for example with `nano .env`, and add the token:

   ```dotenv
   TELEGRAM_BOT_TOKEN=paste_your_token_here
   TELEGRAM_POLL_TIMEOUT=30
   NOMINATIM_USER_AGENT=telegram-user-info-bot/1.0
   NOMINATIM_LANGUAGE=en
   ```

   In `nano`, press `Ctrl+O`, Enter to save, and then `Ctrl+X` to exit.
5. Start the bot from the activated virtual environment:

   ```bash
   python bot.py
   ```

6. Wait for `Bot @your_bot_name started` in the terminal.
7. Open the bot in Telegram using one of these methods:

   - select the `t.me/your_bot_name` link in the BotFather message;
   - search Telegram for the bot's exact `@username`;
   - send `/mybots` to BotFather if you do not remember the username.

8. Select **Start** or send `/me`. The bot responds with a text report. Use
   `/share` to voluntarily share a phone number or location.

Stop the bot with `Ctrl+C`. It uses long polling and does not save reports or
shared data to disk.

Never share the token because it grants full control over the bot. If a token is
exposed, revoke it through BotFather and generate a new one.

## Updating an existing installation

Do not copy `.env.example` again if `.env` already contains the bot token.

1. Stop the running bot with `Ctrl+C`.
2. Run these commands from the repository root:

   ```bash
   cd telegram-user-info-bot
   python3.11 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   python bot.py
   ```

3. Wait for `Bot @your_bot_name started`.
4. Open a private chat with the bot and send `/share`.
5. Select **Share location** and confirm the action in Telegram.
6. The bot displays the city, country, address, time zone, UTC offset, and local
   time, and then sends the complete text report.

If the place or time zone cannot be determined, the bot still sends the report
and a separate message describing the error.

## Tests

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

GitHub Actions also runs the tests when a pull request is opened or reopened and
whenever new commits are added to it.

## Logs and errors

The bot logs startup, received updates, commands, safe Telegram API method names,
report generation stages, and stack traces for errors. Tokens, phone numbers,
coordinates, and profile contents are not written to the logs.

If an optional report section is unavailable, the bot still sends the report
and then posts a warning listing the errors. If the entire request fails, the
user receives a message containing the request reference, while full technical
details remain in the console.
