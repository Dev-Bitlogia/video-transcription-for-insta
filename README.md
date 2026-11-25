# Video Script Extractor - GitHub + n8n + PDF by email

One Reel at a time. URL typed in n8n. No Sheet. Schedule decided later.
PDF sent by email.

## Files in this folder
- `extractor.py` - same v5 extractor as the web app (Song shows only "Song", no lyrics)
- `cli_extract.py` - one-shot script: takes --url, runs extraction, builds PDF
- `requirements.txt` - requests, openai, reportlab (no FastAPI, no web server)
- `.github/workflows/extract.yml` - GitHub Actions workflow, manual run with reel_url input
- `n8n-workflow.json` - n8n workflow: Form Trigger -> GitHub dispatch -> Wait -> Get PDF -> Gmail send
- `.gitignore` - blocks results/ and .env

## 1. GitHub setup (once)
1. Create a new PRIVATE repo, for example `video-script-extractor`.
2. Upload all files in this folder, keeping the folder structure, including `.github/workflows/extract.yml`.
3. In repo Settings -> Secrets and variables -> Actions -> New repository secret, add 3 secrets:
   - `APIFY_TOKEN`
   - `ASSEMBLYAI_API_KEY`
   - `OPENAI_API_KEY`
   Never commit these keys in code.
4. Test manually in GitHub: Actions -> Extract Reel to PDF -> Run workflow -> paste `https://www.instagram.com/p/DfHXgY2CY2b/` -> Run.
   Wait 2-3 minutes. Download the artifact `reel-report`. It should contain `<shortcode>.pdf` and `<shortcode>.json`.
   Expected PDF: Spoken script = "Song", Verdict = "Not viral - only 420 views".

## 2. n8n setup (once)
1. In n8n, import `n8n-workflow.json`.
2. Replace these placeholders in the workflow:
   - `REPLACE_WITH_OWNER` - your GitHub username / org, for example `Dev-Bitlogia`
   - `REPLACE_WITH_REPO` - repo name, for example `video-script-extractor`
   - GitHub PAT credential - create a Personal Access Token in GitHub Settings -> Developer settings -> Personal access tokens, with `repo` and `workflow` scope. Save it as an n8n HTTP Header Auth credential, header `Authorization: Bearer <PAT>`. Never paste the PAT in chat.
   - Gmail credential - connect your Gmail in n8n, as in your other workflows.
   - `REPLACE_WITH_EMAIL` - the email address that should receive the PDF.
3. Open the Form Trigger URL in n8n, type one Reel URL, submit.
4. n8n will: start GitHub, wait 3 minutes, download the PDF artifact, and send it by Gmail with the PDF attached.
   Subject: `Reel report - <shortcode>`
   If GitHub is not finished after 3 minutes, the workflow will send an email saying "GitHub still running, check Actions". Run it again after 2 minutes, or increase the Wait node to 5 minutes.

## 3. Schedule (decide later)
Right now the workflow starts only when you submit the form in n8n.
Later, when you decide the schedule, add a Schedule Trigger node in n8n, and change the Reel URL source from the form to a fixed URL, a Sheet row, or another trigger. Do not change the GitHub part.

## Cost
- GitHub Actions: $0, free plan 2,000 minutes/month. One Reel is about 2 minutes.
- n8n: $0 extra, on your Bitlogia server.
- API cost: about $0.12 per Reel, same as localhost.
