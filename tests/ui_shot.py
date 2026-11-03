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
    x, y, w, h = app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()
    ImageGrab.grab((x, y, x + w, y + h)).save(sys.argv[1])
    app.apply_status_preset("artist_first"); app.update(); time.sleep(0.3)
    ImageGrab.grab((x, y, x + w, y + h)).save(sys.argv[1].replace(".png", "-2.png"))
    app.toggle_custom(); app.update(); time.sleep(0.3)
    ImageGrab.grab((x, y, x + w, y + h)).save(sys.argv[1].replace(".png", "-3.png"))
    app.quit_app()
app.after(2500, shot)
app.mainloop()
