/**
 * CX Analytics — Gmail → GitHub Automation
 *
 * This Google Apps Script watches for Zendesk Explore emails,
 * extracts the zip attachment, and commits it to the GitHub repo.
 * The commit triggers a GitHub Actions workflow that builds the dashboard.
 *
 * Setup:
 * 1. Create a new Google Apps Script project at script.google.com
 * 2. Paste this code into Code.gs
 * 3. Set Script Properties (Project Settings → Script Properties):
 *    - GITHUB_TOKEN: Personal access token with `repo` scope
 *    - GITHUB_REPO: "jakegumabon/cxdashboard"
 * 4. Run `setup()` once to create the time-based trigger
 */

// ── Configuration ──────────────────────────────────────────────────────────

const CONFIG = {
  // Gmail search query for Zendesk Explore emails
  GMAIL_QUERY: 'from:no-reply@zendeskexplore.com subject:"Your delivery of For Claude" has:attachment newer_than:2d',

  // Gmail label to mark processed emails (auto-created)
  PROCESSED_LABEL: "CX-Dashboard/Processed",

  // GitHub target path for the zip file
  GITHUB_ZIP_PATH: "data/latest.zip",

  // GitHub branch to commit to
  GITHUB_BRANCH: "main",

  // How often to check (in minutes)
  CHECK_INTERVAL_MINUTES: 30,
};


// ── Main entry point ───────────────────────────────────────────────────────

/**
 * Main function — called by time trigger.
 * Searches Gmail for new Zendesk Explore emails, extracts the zip,
 * and commits it to GitHub.
 */
function checkForNewReport() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty("GITHUB_TOKEN");
  const repo = props.getProperty("GITHUB_REPO");

  if (!token || !repo) {
    console.error("Missing GITHUB_TOKEN or GITHUB_REPO in Script Properties");
    return;
  }

  // Get or create the "processed" label
  const label = getOrCreateLabel(CONFIG.PROCESSED_LABEL);
  const labelId = label.getName();

  // Search for matching emails NOT already labeled as processed
  const query = `${CONFIG.GMAIL_QUERY} -label:${labelId.replace(/\//g, "-")}`;
  const threads = GmailApp.search(query, 0, 5);

  if (threads.length === 0) {
    console.log("No new Zendesk Explore emails found.");
    return;
  }

  // Process the most recent thread only
  const thread = threads[0];
  const messages = thread.getMessages();
  const latestMessage = messages[messages.length - 1];

  console.log(`Processing email: "${latestMessage.getSubject()}" from ${latestMessage.getDate()}`);

  // Find the zip attachment
  const attachments = latestMessage.getAttachments();
  const zipAttachment = attachments.find(a =>
    a.getContentType() === "application/zip" ||
    a.getName().toLowerCase().endsWith(".zip")
  );

  if (!zipAttachment) {
    console.error("No zip attachment found in the email.");
    thread.addLabel(label); // Mark as processed to avoid retrying
    return;
  }

  console.log(`Found zip: ${zipAttachment.getName()} (${zipAttachment.getSize()} bytes)`);

  // Commit to GitHub
  const zipBase64 = Utilities.base64Encode(zipAttachment.getBytes());
  const success = commitToGitHub(token, repo, CONFIG.GITHUB_ZIP_PATH, zipBase64, zipAttachment.getName());

  if (success) {
    // Mark email as processed
    thread.addLabel(label);
    console.log("Successfully committed zip to GitHub and marked email as processed.");
  } else {
    console.error("Failed to commit zip to GitHub.");
  }
}


// ── GitHub API ─────────────────────────────────────────────────────────────

/**
 * Commit a base64-encoded file to GitHub via the Contents API.
 */
function commitToGitHub(token, repo, path, contentBase64, originalFilename) {
  const apiUrl = `https://api.github.com/repos/${repo}/contents/${path}`;
  const now = new Date().toISOString();
  const commitMessage = `data: update Zendesk export (${originalFilename}) — ${now}`;

  // Check if file already exists (need SHA to update)
  let sha = null;
  try {
    const getResp = UrlFetchApp.fetch(apiUrl, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github.v3+json",
      },
      muteHttpExceptions: true,
    });
    if (getResp.getResponseCode() === 200) {
      sha = JSON.parse(getResp.getContentText()).sha;
    }
  } catch (e) {
    console.log("File does not exist yet, creating new.");
  }

  // Create or update the file
  const payload = {
    message: commitMessage,
    content: contentBase64,
    branch: CONFIG.GITHUB_BRANCH,
  };
  if (sha) {
    payload.sha = sha;
  }

  try {
    const putResp = UrlFetchApp.fetch(apiUrl, {
      method: "PUT",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github.v3+json",
        "Content-Type": "application/json",
      },
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
    });

    const code = putResp.getResponseCode();
    if (code === 200 || code === 201) {
      console.log(`GitHub commit successful (${code})`);
      return true;
    } else {
      console.error(`GitHub API error ${code}: ${putResp.getContentText()}`);
      return false;
    }
  } catch (e) {
    console.error(`GitHub API exception: ${e.message}`);
    return false;
  }
}


// ── Gmail helpers ──────────────────────────────────────────────────────────

/**
 * Get or create a Gmail label (supports nested labels like "Folder/Sub").
 */
function getOrCreateLabel(labelName) {
  let label = GmailApp.getUserLabelByName(labelName);
  if (!label) {
    label = GmailApp.createLabel(labelName);
    console.log(`Created Gmail label: ${labelName}`);
  }
  return label;
}


// ── Setup & teardown ───────────────────────────────────────────────────────

/**
 * Run this once to set up the time-based trigger.
 */
function setup() {
  // Remove any existing triggers for this function
  teardown();

  // Create a new trigger that runs every N minutes
  ScriptApp.newTrigger("checkForNewReport")
    .timeBased()
    .everyMinutes(CONFIG.CHECK_INTERVAL_MINUTES)
    .create();

  console.log(`Trigger created: checkForNewReport runs every ${CONFIG.CHECK_INTERVAL_MINUTES} minutes`);

  // Also ensure the label exists
  getOrCreateLabel(CONFIG.PROCESSED_LABEL);
  console.log("Setup complete. Make sure GITHUB_TOKEN and GITHUB_REPO are set in Script Properties.");
}

/**
 * Remove all triggers for checkForNewReport.
 */
function teardown() {
  const triggers = ScriptApp.getProjectTriggers();
  triggers.forEach(trigger => {
    if (trigger.getHandlerFunction() === "checkForNewReport") {
      ScriptApp.deleteTrigger(trigger);
      console.log("Removed existing trigger for checkForNewReport");
    }
  });
}

/**
 * Manual test — run this to test the pipeline without waiting for the trigger.
 */
function testRun() {
  checkForNewReport();
}
