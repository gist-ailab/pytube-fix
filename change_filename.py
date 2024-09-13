import os

file_list = os.listdir('/home/bak/Projects/pytube/download_path/srt-files')
file_list.sort()

for i in file_list:
    old_name = os.path.join('/home/bak/Projects/pytube/download_path/srt-files', i)
    j = i.replace(' (en.j3PyPqV-e1s)', '')
    new_name = os.path.join('/home/bak/Projects/pytube/download_path/srt-files', j)

    os.rename(old_name, new_name)