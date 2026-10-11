# How to run Regime on your computer

Regime is a trading agent for tokenized stocks. These steps start it in **practice mode**. Practice mode is called SIM. It uses sample prices and pretend money. You do not need a wallet, a password, or a Binance account.

## 1. Install three free programs

Install these once. During the Python install on Windows, tick **Add python.exe to PATH**.

1. Git: https://git-scm.com/downloads
2. Python 3.11 or newer: https://www.python.org/downloads/
3. Node.js 20 or newer: https://nodejs.org/

Close the installer windows. Open a new **PowerShell** window (Windows) or Terminal (Mac).

Check that they work. Type each line and press Enter:

```powershell
git --version
python --version
node --version
```

You should see a version number for each one. If `python` opens the Microsoft Store, install Python again from the link above and tick **Add python.exe to PATH**.

## 2. Download Regime

```powershell
git clone https://github.com/rishu4436/regime.git
cd regime
```

## 3. Start it

**Windows**

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\backend\app\deploy\run.ps1
```

The first line is only needed if Windows says running scripts is disabled. It lasts for this window only.

**Mac or Linux**

```bash
backend/app/deploy/run.sh
```

The first start can take a few minutes. It installs the Python and dashboard packages for you. Leave this window open. When it is ready it prints:

`Regime dashboard: http://localhost:5173`

## 4. Open the dashboard

In your browser go to:

http://localhost:5173

You should see the Regime page. The mode says **SIM**. That means practice mode.

Try this:

1. Open **Auto Mode**.
2. Pick **Balanced**.
3. Turn the agent on.
4. Go back to the **Dashboard**.
5. Read the reason under the latest decision. Regime says why it bought, sold, or stayed out.

You can also open **Manual Builder**, make a simple rule, and press **Backtest** to see that rule on old prices.

Nothing on this page spends real money.

## 5. Stop it

Click the terminal window and press **Ctrl+C**.

## If something goes wrong

- **The page will not open.** Look at the terminal. Wait until it prints `http://localhost:5173`. Then refresh the browser.
- **`python` is not recognized.** Install Python again and tick **Add python.exe to PATH**. Open a new PowerShell window.
- **`running scripts is disabled`.** Run `Set-ExecutionPolicy -Scope Process Bypass` in that same window, then run the script again.
- **Port already in use.** Another Regime window is still running. Press Ctrl+C there, or restart the computer, then start again.

## What not to do

Do not paste a private key, a seed phrase, or an API secret into a chat, a form, or a file you commit. Practice mode does not ask for any of those.

Real trading is a later step. Read `backend/app/GO_LIVE.md` only when you mean to use real money. Copy `.env.example` to `.env` for that. Never commit `.env`.
