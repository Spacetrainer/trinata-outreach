name: Find Contacts (Daily)

on:
  schedule:
    - cron: "15 9 * * *"
  workflow_dispatch:
    inputs:
      max_rows:
        description: "How many businesses to check this run"
        required: false
        default: "10"

concurrency:
  group: find-contacts
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  find-contacts:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v7

      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"

      - run: pip install requests "gspread>=6.0,<7" google-auth

      - run: python find_contacts.py
        env:
          GOOGLE_SERVICE_ACCOUNT_KEY: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_KEY }}
          GOOGLE_SHEET_ID: ${{ secrets.GOOGLE_SHEET_ID }}
          HUNTER_API_KEY: ${{ secrets.HUNTER_API_KEY }}
          APOLLO_API_KEY: ${{ secrets.APOLLO_API_KEY }}
          MAX_ROWS_PER_RUN: ${{ github.event.inputs.max_rows }}
