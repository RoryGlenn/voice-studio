# Narrate text in the browser

For an installed voice and model, create a preview, listen, then generate the full recording.
Complete [installation](installation.md) first on a new machine.

## Generate your first recording

Start with the configured Python environment, models, and saved voice profile.
On Ubuntu, open a terminal and enter your checkout (the installation guide uses
`~/repos/voice-studio`):

```sh
cd ~/repos/voice-studio
./launch-voice-studio.sh
```

On macOS, use `Launch Voice Studio.command`. If the browser does not open, use
the local URL printed by the launcher, including its session token. Keep that
URL private. Do not substitute the audiobook dashboard URL: they are different
interfaces.

1. Paste your text into **The script**, or choose **Import .txt**.
2. Select a **Narrator** and an available voice engine.
3. Choose **Preview first 120 words** and listen before starting a long render.
4. Adjust the available voice, speed, and pause controls as needed, then choose
   **Generate audio**.
5. Play the completed recording and download WAV or MP3. WAV preserves
   uncompressed audio; MP3 uses less storage.

Use **Cancel render** to stop a browser render, or **Quit studio** when finished.
Studio and audiobook workers share a GPU lock. If another narration worker owns
it, wait for that worker or stop its service before generating in Studio.

## Choose a narrator

A narrator is a saved voice reference; an engine is the model that produces its
speech. Choose the narrator first, then one of that profile's available engines.
The application applies the narrator's saved defaults. If a required model or
reference is missing, the engine is unavailable; it does not silently switch to
another model.

Natural is the engine used for EPUB books. Expressive is an optional separately
installed engine with an expression control; Natural does not expose that slider.
Preview the same text after a change so you can judge the audible result.

To add a voice, follow [voice profiles](reference.md#voice-profiles). Restart
Studio after adding or removing profiles. Removing a profile does not erase past
recordings or change the voice frozen in a prepared audiobook.

## Choose an audio format

WAV exports use uncompressed 24-bit PCM at the model's native 24 kHz sample rate.
MP3 uses less space. Extra export bit depth preserves available precision but
cannot create missing source detail. Play the export before treating it as a
finished recording.

See [troubleshooting](operations.md#understand-progress-and-recover-from-a-stop)
for competing workers, unavailable GPU capacity, and stopped jobs.
