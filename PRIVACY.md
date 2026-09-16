# Privacy policy for plaud-scribe

Last updated: 16 September 2026

plaud-scribe is a personal, single-user tool. It is run by one individual on their own
server, against their own accounts. It is not offered as a service to anyone else, has no
other users, and collects nothing from visitors to this repository.

## Who operates it

Grzegorz Górkiewicz. Contact: gorkiewicz.grzegorz@gmail.com

## What it does with data

The tool takes voice recordings made by the operator on their own Plaud device, produces
a text transcript and a written summary of each one, and files the results in the
operator's own Google Drive.

The only person whose data it processes is the operator.

## What is sent where

| Destination | What is sent | Why |
| --- | --- | --- |
| ElevenLabs | A time-limited link to the audio, or the audio itself if that link fails | To produce the transcript |
| Anthropic | The text of the transcript | To produce the summary |
| Google Drive | The transcript and summary, as files | To store the results |

In the normal path, audio is never downloaded to the server: Plaud issues a temporary
link and ElevenLabs reads it directly. If that fails, the audio is downloaded to a
temporary directory, sent to ElevenLabs, and deleted immediately afterwards.

Each provider handles what it receives under its own terms: ElevenLabs, Anthropic and
Google respectively.

## Google account access

The tool requests exactly one Google scope, `drive.file`. That scope grants access only
to files the tool itself creates. It cannot read, modify or see any other file in the
operator's Google Drive, and no Google account data is sent anywhere else.

The OAuth token is stored with owner-only permissions on the operator's own server. Access
can be withdrawn at any time at https://myaccount.google.com/permissions.

## What is kept, and for how long

Transcripts, summaries and their cached source data are stored on the operator's own
server and in the operator's own Google Drive, for as long as the operator chooses to keep
them. Deleting the files removes them. Nothing is retained anywhere else by this tool.

## What is not done

No data is sold, shared, or used for advertising. Nothing is used to train any model by
this tool. There are no analytics, no tracking, and no third-party recipients beyond the
three named above.

## Changes

Any change to this policy will be committed to this repository, where its history is
publicly visible.
