#NoEnv
#SingleInstance force
SendMode Input

F6:: Pause, Toggle

F3 UP::
Loop
{
Send, {W DOWN}
Sleep, 5000
Send, {W UP}
Sleep, 100
Send, {Space DOWN}
sleep, 2000
Send, {Space UP}
sleep, 100
Send, {A DOWN}
Sleep, 3000
Send, {A UP}
Sleep, 100
Send {LBUTTON Down}
sleep, 35
Send {LBUTTON UP}
sleep, 3000
Send, {W DOWN}
Sleep, 5000
Send, {W UP}
Sleep, 100
Send, {Space DOWN}
sleep, 2000
Send, {Space UP}
sleep, 100
Send, {D DOWN}
Sleep, 3000
Send, {D UP}
Sleep, 100
Send {LBUTTON Down}
sleep, 35
Send {LBUTTON UP}
sleep, 3000
}