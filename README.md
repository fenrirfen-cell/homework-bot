# Homework bot

Every morning this bot logs in to Brightspace, finds anything due in the next 2 days
(assignments, quizzes and discussions), and sends each one to your phone with its
instructions. Every Sunday evening it sends the whole week ahead.
Assignments you've already turned in are skipped.

It runs for free on GitHub's servers, so your computer doesn't need to be on.

## Setup (about 10 minutes)

### 1. Get the ntfy app on your phone
1. Install **ntfy** from the App Store or Google Play.
2. Tap **+**, and subscribe to your topic name (use exactly this, keep it private):
   `homework-0e36e2d1f1fcba54`

### 2. Add your settings to GitHub
In this repository go to **Settings → Secrets and variables → Actions → New repository secret**
and add these four:

| Name | What to put |
|---|---|
| `BRIGHTSPACE_URL` | Your Brightspace address, e.g. `https://yourschool.brightspace.com` |
| `BRIGHTSPACE_USERNAME` | Your school username or email |
| `BRIGHTSPACE_PASSWORD` | Your school password |
| `NTFY_TOPIC` | `homework-0e36e2d1f1fcba54` |

Secrets are encrypted. Nobody can read them after you save them, not even you.

### 3. Test it
1. Go to the **Actions** tab. If asked, tap **I understand… enable them**.
2. Tap **Homework bot → Run workflow**, leave it on **test**, and tap **Run workflow**.
3. In 2–3 minutes you should get "✅ Homework bot is working" on your phone.

If it fails you'll get a ⚠️ notification. Open the failed run and download **debug** at
the bottom to see a screenshot of where it got stuck.

## Good to know
- GitHub may run the schedule a few minutes late.
- GitHub pauses schedules after 60 days with no changes to the repository. If the
  notifications stop, open the Actions tab and turn the workflow back on.
- To change the times, edit the two `cron:` lines in `.github/workflows/homework.yml`
  (times are in UTC; Eastern is UTC−5 in winter).
- Anyone who knows your topic name could read your notifications, so don't share it.
