# Client Finder Assistant

A desktop assistant that finds potential clients for a web, app, AI and data
freelancer. It runs on your own Windows PC; your leads, settings and API keys
never leave it.

**Stage 1 (this version)** finds businesses, audits their websites and builds
your lead list:

- Type a kind of business and a place, for example *dentists* in *Houston, Texas*.
- The app finds matching businesses through Google Places.
- It reads each business's public website and lists what could be improved:
  slow loading, no mobile layout, weak Google visibility, outdated build, no
  enquiry form, no online booking, no chat, no visitor tracking, broken links,
  or no website at all.
- It collects the phone number and email addresses the business publishes.
- Each lead gets an **opportunity score** (0–100) and a list of upgrade ideas
  matched to your services.
- You shortlist leads, add notes, track them from *new* to *won*, and export
  the list to a spreadsheet.

Still to come: AI-written cold emails that you approve before sending
(stage 2), voice control (stage 3), and a finder for paid projects on job
boards (stage 4).

## Set up (once)

1. **Install Python.** Download it from <https://www.python.org/downloads/>.
   In the installer, tick **Add python.exe to PATH**.
2. **Get the app.** On the GitHub page press **Code → Download ZIP** and
   unzip it, for example to `Documents\client-finder-assistant`.
   (If you use Git, clone the repo instead; `update.bat` can then fetch new
   versions for you.)
3. **Start it.** Double-click `start.bat`. The first run takes a minute or two
   while it sets itself up. A black window stays open, and the app opens in
   its own window. Close the black window to stop the app.
4. **Add your Google Places key.** In the app, open **Settings**, paste the
   key, press **Save**, then **Test Google key**.

### Getting a Google Places key

1. Sign in at <https://console.cloud.google.com/> and create a project.
2. Turn on billing for the project (Google requires a payment card).
3. Enable the API called **Places API (New)**.
4. Create an **API key** on the project's Credentials page.
5. Optional: also enable **PageSpeed Insights API** if you want Google's
   mobile speed score for each site (tick the option in Settings).

Google's reference for the search this app uses:
<https://developers.google.com/maps/documentation/places/web-service/text-search>

**Cost.** Google charges per search request. One request returns up to 20
businesses, so a search for 20 leads is one request and a search for 60 is
three. The app asks for phone numbers and websites, which Google bills at its
higher "Enterprise" rate. Check Google's current prices and free allowance,
and set a budget alert in Google Cloud.

## Using it

- **Find** – run a search, or paste one website to audit it on its own.
- **Leads** – everything found so far. Sort by best opportunity, filter, open
  a lead to see its issues and upgrade ideas, set its status, add notes,
  export to CSV.
- **Settings** – your details, your services and portfolio, and your keys.

Tips:

- Be specific: "family dentists in Leeds" beats "dentists in UK".
- Google returns at most 60 businesses per search. To cover a big city, search
  by district.
- A business with **no website** scores high on purpose: it needs one.
- "Site blocks bots" means the site asked automated tools to stay out, and
  the app respected that. Open the site yourself to judge it.

## What the audit does and does not do

The audit loads a site's homepage, up to two contact or about pages, and a
handful of its own links, the same pages any visitor can open. It identifies
itself honestly, obeys each site's `robots.txt`, and stops there.

It does **not** try passwords, hidden pages, forms or anything else a normal
visitor could not see. It reports visible weaknesses, not security holes.
Testing someone's site for security holes without their written permission is
illegal in most countries.

Two limits to keep in mind:

- "Load time" is how long the server took to send the page to *your* PC. A
  site hosted far from you looks slower than it does to its local customers.
  Turn on the PageSpeed option for Google's own measurement.
- The checks are automatic and sometimes wrong. Open the site and confirm an
  issue with your own eyes before you mention it to the business.

## Your data

- `data/settings.json` holds your details and API keys.
- `data/leads.db` holds your leads.

Both stay on your PC and are excluded from Git, so they are never uploaded to
GitHub. Back up the `data` folder if your leads matter to you. When you
replace the app with a newer ZIP, copy your `data` folder into the new one.

Google's Maps Platform terms limit how long details returned by Places may be
stored. Read them before treating Google's listing data as a permanent
database, and prefer the contact details published on the business's own site.

## Updating

- **Downloaded as ZIP:** download the new ZIP, unzip it, and copy your `data`
  folder across.
- **Cloned with Git:** double-click `update.bat`.

## If something goes wrong

| What you see | What to do |
|---|---|
| "Python is not installed" | Install Python and tick **Add python.exe to PATH**, then run `start.bat` again. |
| The Microsoft Store opens when you start the app | Windows is offering its own Python. Install Python from python.org instead. |
| "Google rejected the API key" | Copy the key again in full into Settings. |
| "Places API (New) is not switched on" | Enable **Places API (New)** in Google Cloud, wait a minute, retry. |
| Many leads say "Could not read site" | Check your internet connection, then press **Re-audit** on a lead. |
| The app window does not open | Open <http://127.0.0.1:8765> in Chrome or Edge while the black window is running. |

## For developers

Python 3.10+, FastAPI, SQLite, and a plain HTML/CSS/JS front end with no
build step.

```
app/main.py      local web server and routes
app/places.py    Google Places Text Search client
app/audit.py     website audit, issues, ideas, score
app/contacts.py  public email / phone / social extraction
app/jobs.py      background searches and audits
app/db.py        SQLite storage
app/config.py    local settings
app/static/      the app window
docs/API.md      contract between server and window
tests/           pytest suite (no network needed)
```

Run the tests with `pip install pytest` then `python -m pytest`.
