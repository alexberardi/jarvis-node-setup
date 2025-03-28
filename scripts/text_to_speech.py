import subprocess


def speak(text):
    print(f"Speaking: {text}")
    subprocess.run(
        # f'espeak -a 20 -v en-us -s 130 "{text}" --stdout | aplay -r 44100',
        f"flite -voice awb -t {text}",
        shell=True,
    )


if __name__ == "__main__":
    speak("Hello! I am ready.")
