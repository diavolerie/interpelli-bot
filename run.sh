#!/data/data/com.termux/files/usr/bin/bash
   cd ~/interpelli-bot-main
   export TELEGRAM_BOT_TOKEN="INSERISCI_IL_TOKEN"
   export TELEGRAM_CHAT_ID="INSERISCI_CHAT_ID"
   git pull --rebase origin main
   python monitor_interpelli.py >> run_locale.log 2>&1
   git add state.json
   if ! git diff --cached --quiet; then
     git commit -m "Update state.json [skip ci]"
     git pull --rebase origin main
     git push
   fi
