# DiscordQuestBypass

a simple app to complets discord quest without having to own/play a game for 15 minute
do not work on video quests or activity quests

# showcase


https://github.com/user-attachments/assets/4dca666d-46ee-47a7-aec7-32ed4ff2b22d



# how to use
1 first download "DiscordQuestCompleter-windows.zip" on the release page, unzip it anywhere, and run DiscordQuestCompleter.exe inside the folder (keep the whole folder together, the exe needs the files next to it)
2 select "update library" and wait a seconde
3 select "select game" then the game needed for the discord quest
4 you can close the launcher but do not close the program utile the quest is finish

# QnA
1 why do the window look wierd after selecting a game
its because the "game bypass" use coloring that is not supported by default command line window so download windows terminal to fix this easly

2 where are all the datas located
they are at c:\user\{current user}\appdata\roaming\DiscordQuestCompleter

3 a game is missing from the list what can i do
if the game is missing well use the "update library" and if it is still not here wait abit utile i add it to the list "usually take between 2min to 12h if i sleep"

4 the program crash or give me errors
well please open a ticket in the "issues" github pages and ill try to fix it when i got some time 

5 windows defender / smartscreen says its a trojan or virus
its a false positive. the exe is built from the Launcher.py / Default.py in this repo with pyinstaller, and pyinstaller apps get flagged a lot because malware uses pyinstaller too. the code is open here so you can read exactly what it does. if you want to be sure:
- check the sha-256 of your download against the SHA256SUMS.txt on the release page (see "verifying your download" below)
- the release files are built by github actions and signed with a provenance attestation, so you can verify they really came from this repo
if defender already quarantined it you can restore the file and, if you must, add an exclusion for that specific file only (do NOT exclude the whole appdata folder)

# verifying your download
every release includes a SHA256SUMS.txt and a build provenance attestation.

check the hash (powershell):
  Get-FileHash -Algorithm SHA256 .\DiscordQuestCompleter-windows.zip
compare it to the line in SHA256SUMS.txt.

verify it was built by this repo (needs github cli):
  gh attestation verify .\DiscordQuestCompleter-windows.zip --repo vaaanir/DiscordQuestBypass
  gh attestation verify .\default.exe --repo vaaanir/DiscordQuestBypass



# trigger warning 
ai has been used in this project for the following
1 the rainbow effect of the executable file
2 fixing bug
3 help with making the UI cuz i suck at codding
4 fill my lazy ass's code

# ganmes that dont work
- forza horizon 6
- goals
