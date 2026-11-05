# hermes-phone DEMO (60 to 90 s)

Setup: Hermes on Telegram on phone A (screen recorded). Phone B is the developer's second phone, played by a friend acting as "Bella Napoli" host. Split screen: Telegram left, phone B camera/audio right. Real call over Twilio, number on the allowlist.

## Beat 1: ask and approve (0 to 20 s)
- User types: "Call Bella Napoli and book a table for 4 tonight at 8."
- Agent: finds the number (show it says "number from bellanapoli.example/contact"; for the demo the restaurant listing points to phone B or the user gives it).
- Brief card appears: who, number, goal, opening line with AI disclosure, will share: name, party size, time; won't share: card, address. Buttons Call / Edit / Cancel. Tap **Call**.
- Caption: "Nothing is dialed until you tap Call."

## Beat 2: the call and a live question (20 to 65 s)
- Phone B rings. Agent: "Hi, this is an AI assistant calling on behalf of Winch. I'd like to book a table for four tonight at 8 pm."
- Host: "Sure. Inside or on the terrace?"
- Agent: "One moment please, let me check."
- Telegram: question card "They ask: inside or terrace? Suggested: terrace (from memory: you like outdoor seating)". Tap the suggestion.
- Agent: "Terrace, please." Host: "Name for the booking?" Agent answers "Winch" from the brief. Host: "Confirmation is B-4-7."
- Caption: "It asks you instead of guessing."

## Beat 3: result (65 to 85 s)
- Telegram result: "Success. Table for 4, tonight 20:00, terrace, under Winch. Confirmation B47. Next: nothing. 1m 12s, about $0.30." Tap "Transcript" to show it scroll.
- Optional 5 s: try "call 911" and show the refusal.
- End card: "hermes-phone. Approve first. Discloses AI. Asks you mid-call."

Recording tips: disable notifications, lower Twilio latency by running the bridge close to the tunnel region, record call audio from phone B speaker with a separate mic.

## README pitch (top of README.md)

> **hermes-phone** lets your Hermes agent make the phone calls you keep putting off. Say "book a table" or "ask the clinic if they have a slot Friday", check the brief, tap Call. The agent introduces itself as an AI, handles the conversation, and when the other side asks something it doesn't know, it puts them on a polite hold and asks you in chat, with a suggested answer ready. You get the result, the confirmation number and the transcript. No call without your approval, no surprise sharing: card, address and ID only with per-call consent (CVV, passwords and one-time codes never), no auto-redials, emergency numbers blocked.
>
> Twilio Media Streams + OpenAI Realtime, native Hermes hooks, slash commands and live messages. Works from Telegram, Discord, Slack or the CLI.
