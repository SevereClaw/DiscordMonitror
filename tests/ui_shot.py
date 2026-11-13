# Скриншот вкладки «Статус» (запускается на Windows-сервере GitHub)
import os, sys, time
sys.path.insert(0, os.getcwd())
import vlc_discord as vd
os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)
app = vd.App()
app.geometry("620x900+0+0")
app.show_tab("status")
def shot():
    from PIL import ImageGrab
    app.update()
    wiz = app.open_setup()                      # мастер подключения: снять и убрать с экрана
    app.update(); time.sleep(0.3); app.update()
    ImageGrab.grab((wiz.winfo_rootx(), wiz.winfo_rooty(), wiz.winfo_rootx() + wiz.winfo_width(),
                    wiz.winfo_rooty() + wiz.winfo_height())).save(sys.argv[1].replace(".png", "-setup.png"))
    wiz.close()
    app.update(); time.sleep(0.3); app.update()
    x, y, w, h = app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()
    ImageGrab.grab((x, y, x + w, y + h)).save(sys.argv[1])
    app.apply_status_preset("artist_first"); app.update(); time.sleep(0.3)
    ImageGrab.grab((x, y, x + w, y + h)).save(sys.argv[1].replace(".png", "-2.png"))
    app.toggle_custom(); app.update(); time.sleep(0.3)
    ImageGrab.grab((x, y, x + w, y + h)).save(sys.argv[1].replace(".png", "-3.png"))
    import base64
    for suf in ("", "-2", "-3", "-setup"):
        f = sys.argv[1].replace(".png", suf + ".png")
        print("SHOT", os.path.basename(f), base64.b64encode(open(f, "rb").read()).decode())
    app.quit_app()
app.after(2500, shot)
app.mainloop()
