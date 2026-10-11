# SPDX-License-Identifier: AGPL-3.0-only
"""Saved legend defaults: normalized page areas, collision checks and visual editor."""
import copy
import math
from pathlib import Path
import fitz


def clean_defaults(value):
    result = {}
    if not isinstance(value, dict):
        return result
    for orientation in ('Landscape', 'Portrait'):
        profile = value.get(orientation)
        if not isinstance(profile, dict):
            continue
        try:
            rect = [float(v) for v in profile['rect']]
            font = float(profile['font'])
            if (len(rect) != 4 or not all(math.isfinite(v) for v in rect + [font])
                    or not (0 <= rect[0] < rect[2] <= 1 and 0 <= rect[1] < rect[3] <= 1)
                    or rect[2]-rect[0] < .03 or rect[3]-rect[1] < .03 or not 6 <= font <= 60):
                continue
            result[orientation] = dict(rect=rect, font=font,
                                       anchor='bottom' if profile.get('anchor') == 'bottom' else 'top')
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return result


def orientation_of(width, height):
    return 'Landscape' if width >= height else 'Portrait'


def default_profile():
    return dict(rect=[.65, .025, .975, .375], font=12.0, anchor='top')


def preferred_layout(display, bounds, profile, width_at, height_at):
    """Fit within the user's area at <= preferred font; never cover tracing bounds."""
    orientation = orientation_of(display.width, display.height)
    profile = clean_defaults({orientation: profile}).get(orientation)
    if profile is None:
        return None
    x0,y0,x1,y1 = profile['rect']
    area = fitz.Rect(display.x0+x0*display.width, display.y0+y0*display.height,
                     display.x0+x1*display.width, display.y0+y1*display.height)
    protected = (bounds + fitz.Rect(-12,-12,12,12)) if bounds is not None else None
    font = profile['font']
    while font >= 6:
        height = height_at(font)
        top = area.y1-height if profile['anchor'] == 'bottom' else area.y0
        box = fitz.Rect(area.x0, top, area.x1, top+height)
        if (width_at(font) <= area.width + 1e-6 and height <= area.height + 1e-6
                and (protected is None or not box.intersects(protected))):
            return font, box
        if font == 6:
            break
        font = max(6.0, font-.5)
    return None


def measure_template(template, font):
    m = template['metrics']
    width = max(m['row']*font+12, m['title']*font, m['toolbar']*font)+16
    row = max(font*1.7, font*2.2+2 if m['symbols'] else 0)
    return width, 28 + font*2.5 + len(template['rows'])*row


def sample_template(orientation):
    width,height = (2592,1728) if orientation == 'Landscape' else (1728,2592)
    return dict(width=width, height=height, sheet='Sample drawing (36 x 24 in)',
                rows=[['420 SQ FT', 'Floor finish'], ['146 LN FT', 'Wall base'], ['4 EA', 'Fixtures']],
                metrics=dict(row=210/12, title=250/12, toolbar=230/12, symbols=False),
                bounds=None, manual=False)


class LegendDefaultsDialog:
    """Edit a copy; preferences change only through the Save callback."""
    def __init__(self, parent, defaults, on_save, template=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.value = clean_defaults(defaults)
        self.on_save = on_save
        self.template = template
        self.current = orientation_of(template['width'],template['height']) if template else 'Landscape'
        self.window = tk.Toplevel(parent)
        self.window.title('Default legend layout')
        self.window.geometry(f"{min(1120,parent.winfo_screenwidth()-60)}x{min(800,parent.winfo_screenheight()-80)}")
        self.window.minsize(760,560)
        self.window.transient(parent)
        self.window.grab_set()
        self.window.bind('<Escape>', lambda e: (self.window.destroy(), 'break')[1])
        self.window.columnconfigure(0,weight=1); self.window.rowconfigure(1,weight=1)
        bar = ttk.Frame(self.window,padding=12); bar.grid(row=0,column=0,sticky='ew')
        self.orientation = tk.StringVar(value=self.current)
        choice=ttk.Combobox(bar,values=('Landscape','Portrait'),textvariable=self.orientation,state='readonly',width=12)
        choice.pack(side='left'); choice.bind('<<ComboboxSelected>>',self.switch_orientation)
        self.enabled=tk.BooleanVar()
        ttk.Checkbutton(bar,text='Use my default',variable=self.enabled,command=self.redraw).pack(side='left',padx=14)
        ttk.Label(bar,text='Preferred text (pt)').pack(side='left')
        self.font=tk.StringVar()
        self.font_box=ttk.Spinbox(bar,from_=6,to=60,increment=.5,textvariable=self.font,width=5,command=self.redraw)
        self.font_box.pack(side='left',padx=6); self.font_box.bind('<KeyRelease>', self.redraw)
        self.anchor=tk.StringVar()
        self.zoom=tk.StringVar(value='Fit page')
        zoom_box=ttk.Combobox(bar,values=('Fit page','100%','200%'),textvariable=self.zoom,state='readonly',width=9)
        zoom_box.pack(side='left',padx=8); zoom_box.bind('<<ComboboxSelected>>',self.redraw)
        ttk.Button(bar,text='Reset to automatic',command=self.reset).pack(side='right')
        area=ttk.Frame(self.window); area.grid(row=1,column=0,sticky='nsew',padx=12)
        area.rowconfigure(0,weight=1); area.columnconfigure(0,weight=1)
        self.canvas=tk.Canvas(area,background='#616a74',highlightthickness=0)
        self.canvas.grid(row=0,column=0,sticky='nsew')
        vertical=ttk.Scrollbar(area,command=self.canvas.yview); vertical.grid(row=0,column=1,sticky='ns')
        horizontal=ttk.Scrollbar(area,orient='horizontal',command=self.canvas.xview); horizontal.grid(row=1,column=0,sticky='ew')
        self.canvas.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.canvas.bind('<Configure>',self.redraw)
        self.canvas.bind('<ButtonPress-1>',self.start_drag)
        self.canvas.bind('<B1-Motion>',self.drag)
        self.canvas.bind('<ButtonRelease-1>',lambda e: setattr(self,'dragging',None))
        footer=ttk.Frame(self.window,padding=12); footer.grid(row=2,column=0,sticky='ew')
        corners=ttk.Frame(footer); corners.pack(fill='x')
        ttk.Label(corners,text='Snap to corner:').pack(side='left')
        for label in ('Top left','Top right','Bottom left','Bottom right'):
            ttk.Button(corners,text=label,command=lambda c=label:self.snap(c)).pack(side='left',padx=3)
        ttk.Label(footer,text='Drag the blue area to move it. Drag its lower-right handle to resize.\n'
                  'Text stays on one line and may shrink to fit. A collision uses automatic placement.\n'
                  'Defaults apply across projects; PlanSwift Legend boxes always keep priority.',
                  foreground='#46586b').pack(anchor='w',pady=7)
        self.status=tk.StringVar(); ttk.Label(footer,textvariable=self.status,wraplength=980).pack(anchor='w')
        buttons=ttk.Frame(footer); buttons.pack(fill='x',pady=(8,0))
        ttk.Button(buttons,text='Cancel',command=self.window.destroy).pack(side='right')
        ttk.Button(buttons,text='Save defaults',command=self.save).pack(side='right',padx=8)
        self.dragging=None; self.photo=None; self.background_cache=None
        self.load_profile()

    def load_profile(self):
        profile=self.value.get(self.current,default_profile())
        self.enabled.set(self.current in self.value)
        self.canvas.xview_moveto(0); self.canvas.yview_moveto(0)
        self.rect=list(profile['rect']); self.font.set(f"{profile['font']:g}"); self.anchor.set(profile['anchor'])
        self.redraw()

    def store_profile(self):
        if not self.enabled.get():
            self.value.pop(self.current,None); return True
        valid=clean_defaults({self.current:dict(rect=self.rect,font=self.font.get(),anchor=self.anchor.get())})
        if not valid:
            self.status.set('Enter a text size between 6 and 60 points.'); return False
        self.value.update(valid); return True

    def switch_orientation(self,event=None):
        if not self.store_profile():
            self.orientation.set(self.current); return
        self.current=self.orientation.get(); self.background_cache=None; self.load_profile()

    def active_template(self):
        if self.template and orientation_of(self.template['width'],self.template['height']) == self.current:
            return self.template
        return sample_template(self.current)

    def reset(self):
        self.value.pop(self.current,None); self.load_profile()

    def snap(self, corner):
        width=self.rect[2]-self.rect[0]; height=self.rect[3]-self.rect[1]
        x=.025 if 'left' in corner else .975-width
        y=.025 if 'Top' in corner else .975-height
        x=max(0,min(1-width,x)); y=max(0,min(1-height,y))
        self.rect=[x,y,x+width,y+height]; self.anchor.set('top' if 'Top' in corner else 'bottom')
        self.enabled.set(True); self.redraw()

    def redraw(self,event=None):
        t=self.active_template(); w,h=t['width'],t['height']
        scale=min(max(10,self.canvas.winfo_width()-30)/w,max(10,self.canvas.winfo_height()-30)/h)
        if self.zoom.get() != 'Fit page': scale=float(self.zoom.get().rstrip('%'))/100
        self.canvas.configure(scrollregion=(0,0,w*scale+30,h*scale+36))
        self.mapping=(15,15,w*scale,h*scale)
        self.canvas.delete('all')
        self.canvas.create_rectangle(15,15,15+w*scale,15+h*scale,fill='white',outline='')
        image=t.get('image')
        cache_key=(image,round(w*scale),round(h*scale))
        if image:
            if self.background_cache != cache_key:
                with fitz.open(image) as document:
                    page=document[0]
                    pix=page.get_pixmap(matrix=fitz.Matrix(w*scale/page.rect.width,h*scale/page.rect.height),alpha=False)
                    self.photo=self.tk.PhotoImage(master=self.window,data=pix.tobytes('png'))
                self.background_cache=cache_key
            self.canvas.create_image(15,15,image=self.photo,anchor='nw')
        else:
            self.canvas.create_text(28,30,text=f'{self.current} sample sheet\nUse Legend defaults in Preview PDF to set up on your drawing.',anchor='nw',fill='#718096')
        self.canvas.create_text(18,15+h*scale+1,text=t['sheet'],anchor='nw',fill='white')
        x0,y0,x1,y1=self.rect
        self.area=(15+x0*w*scale,15+y0*h*scale,15+x1*w*scale,15+y1*h*scale)
        self.canvas.create_rectangle(*self.area,outline='#1684e8',width=2,dash=(5,3))
        self.canvas.create_rectangle(self.area[2]-6,self.area[3]-6,self.area[2]+6,self.area[3]+6,fill='#1684e8',outline='white')
        if not self.enabled.get():
            self.status.set('Automatic placement is active for '+self.current.lower()+' sheets.'); return
        try: font=float(self.font.get())
        except ValueError:
            self.status.set('Enter a text size between 6 and 60 points.'); return
        bounds=fitz.Rect(t['bounds']) if t.get('bounds') is not None else None
        layout=preferred_layout(fitz.Rect(0,0,w,h),bounds,dict(rect=self.rect,font=font,anchor=self.anchor.get()),
                                lambda fs:measure_template(t,fs)[0],lambda fs:measure_template(t,fs)[1])
        if layout is None:
            self.status.set('This area cannot fit the legend safely. The export will use automatic placement; widen or move the area.'); return
        fs,box=layout
        box=box*fitz.Matrix(scale,scale); box+=fitz.Rect(15,15,15,15)
        self.canvas.create_rectangle(*box,fill='white',outline='#1684e8',width=2)
        px=max(1,round(fs*scale))
        self.canvas.create_text(box.x0+8*scale,box.y0+8*scale,text='TAKEOFF SUMMARY',anchor='nw',font=('Arial',-px,'bold'))
        self.canvas.create_text(box.x0+8*scale,box.y0+(fs+16)*scale,text='Hide all     Show all     Clear highlight',anchor='nw',font=('Arial',-max(1,round(px*.7))))
        y=box.y0+(fs*2.5+20)*scale
        row_height=max(fs*1.7,fs*2.2+2 if t['metrics']['symbols'] else 0)*scale
        for qty,name in t['rows']:
            self.canvas.create_text(box.x0+8*scale,y,text=f'◉  ■  {qty}   {name}',anchor='nw',font=('Arial',-px))
            y+=row_height
        self.canvas.create_rectangle(*self.area,outline='#1684e8',width=2,dash=(5,3))
        self.canvas.create_rectangle(self.area[2]-6,self.area[3]-6,self.area[2]+6,self.area[3]+6,fill='#1684e8',outline='white')
        suffix=' This page has a PlanSwift Legend box; the default will apply to other pages.' if t.get('manual') else ''
        self.status.set(f'Preferred: {font:g} pt. Fits here at: {fs:g} pt. Area: {(x1-x0)*100:.1f}% × {(y1-y0)*100:.1f}% of the sheet.'+suffix)

    def start_drag(self,event):
        ex,ey=self.canvas.canvasx(event.x),self.canvas.canvasy(event.y)
        x0,y0,x1,y1=self.area
        if abs(ex-x1)<=12 and abs(ey-y1)<=12:
            mode='resize'
        elif x0<=ex<=x1 and y0<=ey<=y1:
            mode='move'
        else: return
        self.dragging=(mode,ex,ey,list(self.rect)); self.enabled.set(True)

    def drag(self,event):
        if not self.dragging: return
        mode,x,y,rect=self.dragging
        dx=(self.canvas.canvasx(event.x)-x)/self.mapping[2]; dy=(self.canvas.canvasy(event.y)-y)/self.mapping[3]
        a,b,c,d=rect
        if mode=='move':
            dx=max(-a,min(1-c,dx)); dy=max(-b,min(1-d,dy)); self.rect=[a+dx,b+dy,c+dx,d+dy]
        else:
            self.rect=[a,b,max(a+.03,min(1,c+dx)),max(b+.03,min(1,d+dy))]
        self.redraw()

    def save(self):
        from tkinter import messagebox
        if not self.store_profile(): return
        try: self.on_save(copy.deepcopy(self.value))
        except (OSError,ValueError) as error:
            messagebox.showerror('Could not save defaults',str(error),parent=self.window); return
        self.window.destroy()
