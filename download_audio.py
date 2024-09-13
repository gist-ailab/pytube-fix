# from pytube import YouTube, Playlist
from pytubefix import YouTube, Playlist
from pytubefix.cli import on_progress
# fix the error: https://github.com/JuanBindez/pytubefix
import argparse
from tqdm import tqdm
import os

parser = argparse.ArgumentParser()
parser.add_argument('--url', '-U', type=str, default='https://music.youtube.com/playlist?list=OLAK5uy_l7gsjJ8ESCrYzQel3ZALXu7yfMpbE4IcY')
args = parser.parse_args()

url = args.url

download_path = 'download_path/Lupin the Third the First'
os.makedirs(download_path, exist_ok=True)


'''download single audio file'''
# audio = YouTube(url,
#                 use_oauth=True, allow_oauth_cache=True)
# print(f'Downloading: {audio.title}')
# audio.streams.filter(only_audio=True).first().download(output_path=download_path, filename=f'{audio.title}.mp3')
# # audio.streams.filter(only_audio=True).first().download(output_path=download_path, filename=f'{"Stay with me"}.mp3')
# # v.streams.filter(only_audio=True).first().download(output_path=download_path, filename=f'{v.title}.mp3')

'''download audio playlist'''
p = Playlist(url)
num = 1

print(f"Downloading: {p.title}")
for audio in tqdm(p.videos, desc='video_download'):
    url = audio.watch_url
    audio = YouTube(url)
    num_str = str(num).zfill(2)

    if '/' in audio.title:
        audio.title = audio.title.replace('/', '_')
    print('\nDownloading: ', f'{num_str}. {audio.title}')
    audio.streams.filter(only_audio=True).first().download(output_path=download_path,
                                                           filename=f'{num_str}. {audio.title}.mp3')
    num += 1
