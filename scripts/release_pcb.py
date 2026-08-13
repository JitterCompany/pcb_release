#!/usr/bin/env python

import sys
import os
import shutil
import datetime
import glob
from pathlib import Path

gerbers_normal = {
        'B_Cu'      : 'Bottom copper',
        'B_Mask'    : 'Bottom soldermask',
        'B_SilkS'   : 'Bottom silkscreen',
        'F_Cu'      : 'Top copper',
        'F_Mask'    : 'Top soldermask',
        'F_SilkS'   : 'Top silkscreen',
        'Edge_Cuts' : 'Board outline',
}

drills_normal = {
        'NPTH'      : 'Non-plated holes',
        'PTH'       : 'Plated holes',
}

gerbers_stencil = {
        'B_Paste'   : 'Bottom paste (stencil)',
        'F_Paste'   : 'Top paste (stencil)'
}

gerbers_fab = {
        'B_Fab'     : 'Bottom side assembly (fab)',
        'F_Fab'     : 'Top side assembly (fab)'
}


def filter_end(list_of_str, end, rm=True):
    end_len = len(end)
    items = [s for s in list_of_str if s.endswith(end)]
    if rm:
        items = [s[:-end_len] for s in items]
    return items

def list_files(path):
    return [f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))]

def confirm(desc):
    if not desc[-1] == '?':
        desc+= " Continue?"
    response = input(desc + " [Y/n] ")
    if not response or response == "y" or response == "Y":
        return True

    print("\nRelease aborted. Quitting...\n")
    sys.exit(0)

def confirm_explicit(desc):
    response = ""
    while not response:
        response = input(desc + " Continue? [y/n] ")
        if response == "y" or response == "Y":
            return True
        if response == 'n' or response == 'N':
            print("\nRelease aborted. Quitting...\n")
            sys.exit(0)
        print("please enter 'Y' or 'N'")

backup_enabled = True
cwd = os.getcwd()
release_dir = cwd + "/production"

# Default: deive project from kicad_pro filename
proj_files = glob.glob('*.kicad_pro')
if len(proj_files) == 1:
    project = Path(proj_files[0]).stem

# Fallback: use name of folder
else:
    project = os.path.basename(cwd)

confirm("\nCreating new release of PCB '{}'".format(project))

if backup_enabled and os.path.isdir(release_dir):
    os.rename(release_dir, release_dir + ".bak-" + datetime.datetime.now().replace(microsecond=0).isoformat())

print("Starting release...\n")
if not os.path.isdir(release_dir):
    os.mkdir(release_dir)

print("Please export gerber files to 'production/' (Pcbnew - plots - plot)".format(cwd))
print("NOTE: If ordering with PCBWay, set disable 'use extended X2 format'")
confirm("Did you export the gerbers?")
print("")
print("Please export drill files to 'production/' (Pcbnew - plots - Generate Drill Files)".format(cwd))
confirm("Did you export the drill files?")
print("")

gerbers = filter_end(list_files(release_dir), ".gbr")
drills = filter_end(list_files(release_dir), ".drl")

board_specs = {
        'asm': [],
        'BOM': None,
        'pos': [],

        'stencil': [],
        'copper': [],

        'drill': [],
        }

# Sort all gerbers in categories
gerb_normal = []
gerb_extra = []
gerb_stencil = []
gerb_fab = []
board_size = {
        'x': '?',
        'y': '?',
        'thickness': '?',
        }
for gerb in gerbers:
    gerb_type = gerb[gerb.rfind('-')+1:]

    # Kicad 6 compatibility
    if gerb_type == 'F_Silkscreen':
        gerb_type = 'F_SilkS'
    if gerb_type == 'B_Silkscreen':
        gerb_type = 'B_SilkS'

    if gerb_type in gerbers_normal:
        gerb_normal.append(gerb_type)
    elif gerb_type in gerbers_stencil:
        gerb_stencil.append(gerb_type)
    elif gerb_type in gerbers_fab:
        gerb_fab.append(gerb_type)
    else:
        gerb_extra.append(gerb_type)

warnings = 0

# Check extra gerber layers
for gerb_type in gerb_extra:
    if gerb_type.endswith("_Cu"):
        print("Assuming layer '{}' is a copper layer...".format(gerb_type))
        board_specs['copper'].append(gerb_type)
    else:
        confirm_explicit("Unrecognised gerber type '{}'.".format(gerb_type))
board_specs['copper'].sort()

# Check minimum-required gerbers for a two-layer board
for ref in gerbers_normal:
    if not ref in gerb_normal:
        warnings+=1
        confirm_explicit("WARNING: missing {}".format(gerbers_normal[ref]))
    elif ref == "F_Cu":
        board_specs['copper'].insert(0, ref)
    elif ref.endswith("_Cu"):
        board_specs['copper'].append(ref)

# Check fabrication layers (component outlines/designators for PCBA)
if not len(gerb_fab):
    confirm("It seems you don't want PCBA (no fab gerbers). Is this correct?")
else:
    if 'F_Fab' in gerb_fab:
        board_specs['asm'].append('top')
    if 'B_Fab' in gerb_fab:
        board_specs['asm'].append('btm')


    if 'btm' in board_specs['asm'] and 'top' in board_specs['asm']:
        print("Detected dual-sided PCBA (top+btm fab layers)")
    elif 'top' in board_specs['asm']:
        confirm("Are you sure you want only top-side PCBA (no btm fab)?")
    else:
        confirm("Are you sure you want only bottom-side PCBA (no top fab)?")

# Check stencils
if not len(gerb_stencil):
    if board_specs['asm']:
        warnings+=1
        confirm_explicit("WARNING: you want PCBA but stencils are missing!")
    else:
        confirm_explicit("Are you sure you don't want stencils?")
else:
    if 'F_Paste' in gerb_stencil:
        board_specs['stencil'].append('top')
    if 'B_Paste' in gerb_stencil:
        board_specs['stencil'].append('btm')

    if 'btm' in board_specs['stencil'] and 'top' in board_specs['stencil']:
        print("Detected dual-sided stencils (top+btm paste layers)")
    elif not 'top' in board_specs['stencil']:
        if 'top' in board_specs['asm']:
            warnings+=1
            confirm_explicit("WARNING: you want top-side PCBA but the stencil is missing!")
        else:
            confirm("Are you sure you don't want a top-side stencil?")
    else:
        if 'btm' in board_specs['asm']:
            warnings+=1
            confirm_explicit("WARNING: you want bottom-side PCBA but the stencil is missing!")
        else:
            confirm("Are you sure you don't want a bottom-side stencil?")

# Check drill files
drill_files = []
for drill in drills:
    drill_type = drill[drill.rfind('-')+1:]
    if not drill_type in drills_normal:
        warnings+=1
        confirm_explicit("WARNING: unrecognised drill file '{}'".format(drill))
    else:
        drill_files.append(drill_type)

for ref in drills_normal:
    if not ref in drill_files:
        warnings+=1
        confirm_explicit("WARNING: missing drill file for {}.".format(drills_normal[ref]))
    else:
        board_specs['drill'].append(ref)


# Assembly requires some extra files
if board_specs['asm']:

    # Add BOM
    BOM_src_file = project + ".xlsx"
    BOM_src_path = cwd + '/' + BOM_src_file
    while True:
        print("Please export the BOM as '{}' in the project root (Eeschema - BOM - Generate)".format(BOM_src_file))
        confirm("Did you export and review the BOM?")
        print("")
        if os.path.isfile(BOM_src_path):
            break;
        else:
            print("{} Not found.".format(BOM_src_path))

    print("Detected BOM")
    BOM_file = 'BOM_' + BOM_src_file
    BOM_dst_path = release_dir + '/' + BOM_file
    shutil.copyfile(BOM_src_path, BOM_dst_path)
    board_specs['BOM'] = BOM_file

    # Add pick&place file
    while True:
        print("Please export the pick&place file to 'production/' (Pcbnew - File - Fabrication outputs - Footprint position)")
        confirm("Did you export and review the pick&place file?")
        print("")
        pos_files = filter_end(list_files(release_dir), "-pos.csv", rm=False)
        if pos_files:
            break;
        else:
            print("No *-pos.csv file(s) found in '{}'".format(release_dir))

    pos_renamed = []
    for pos in pos_files:
        renamed = 'POS_' + pos
        os.rename(release_dir + '/' + pos, release_dir + '/' + renamed)
        pos_renamed.append(renamed)

    board_specs['pos'] = pos_renamed
    print("Detected {} pick&place file(s)".format(len(pos_files)))






readme_file = release_dir + '/README-manufacturing.txt'
with open(readme_file, 'w') as readme:

    readme.write("======\nREADME\n======\n\n")

    readme.write("Copper layers: {}\n".format(len(board_specs['copper'])))
    for i,copper in enumerate(board_specs['copper']):
        readme.write("  - L{}: {}\n".format(i+1, copper))
    readme.write("\n")
    readme.write("{}: {}\n".format(gerbers_normal['Edge_Cuts'], 'Edge_Cuts'))
    readme.write("Board size: {}x{}mm\n".format(board_size['x'], board_size['y']))
    readme.write("Board thickness: {}mm\n".format(board_size['thickness']))
    readme.write("\n")

    readme.write("Drilling: {}\n".format('' if board_specs['drill'] else 'NO'))
    for d in board_specs['drill']:
        readme.write("  - " + d + "\n")
    readme.write("\n")


    if board_specs['stencil']:
        readme.write("Stencils: " + ' + '.join(board_specs['stencil']) + "\n")
        for gerb in gerb_stencil:
            readme.write("  - " + gerb + "\n")
    else:
        readme.write("Stencils: NO\n")
    readme.write("\n")

    if board_specs['asm']:
        readme.write("Assembly: " + ' + '.join(board_specs['asm']) + "\n")
        readme.write("  - Fabrication reference layers:\n")
        for gerb in gerb_fab:
            readme.write("    - " + gerb + "\n")
        readme.write("\n  - BOM: " + board_specs['BOM'] + "\n")
        readme.write("\n  - Pick & Place: " + ' + '.join(board_specs['pos']) + "\n")
    else:
        readme.write("Assembly: NO\n")
    readme.write("\n")

print("\n\nAll design files are complete.")
confirm("Please carefully review/edit the README file:")
os.system("vim \"{}\"".format(readme_file))

print("\nBuilding zip file..")
today_str = datetime.date.today().isoformat()
zipfile="production__{}__{}".format(project, today_str)
shutil.make_archive(zipfile, 'zip', release_dir)

print("\n====== DONE ======")
print("PCB {} released as {}.zip".format(project, zipfile))
if warnings:
    print("Note: {} warnings overruled during release".format(warnings))

print("NOte: don't forget to export the HTML BOM and store the production files on the NAS")


