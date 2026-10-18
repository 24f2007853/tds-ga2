# Q15 - custom Ollama model inside a termlog recording

Before recording: `ollama list` must show `qwen2.5:3b`. Maximize the WSL terminal
window (the grader wants the SYSTEM line on one line), then:

```bash
cd ~/notes/GA2/tds-ga2/q15
termlog rec model.cast
```

Inside the recording, paste these ONE LINE AT A TIME (Enter after each):

```bash
printf '%s\n' 'FROM qwen2.5:3b' 'SYSTEM """You are the deployment assistant for project 34e4166a. Give concise answers and never invent commands."""' 'PARAMETER temperature 0.1' 'PARAMETER num_ctx 2048' 'PARAMETER seed 448' 'PARAMETER num_predict 77' > Modelfile
cat Modelfile
ollama create ga2-assistant-34e4166a -f Modelfile
printf '\nMODELFILE_BEGIN ga2-assistant-34e4166a\n'
ollama show --modelfile ga2-assistant-34e4166a
printf '\nMODELFILE_END ga2-assistant-34e4166a\n'
exit
```

Wait for `success` after `ollama create` before pasting the next line.

After `exit`:

```bash
python3 ../tests/check_cast.py model.cast
tar -czf submission.tar.gz model.cast model.cast.jwt
cd .. && bash publish.sh "Q15 recording"
```
