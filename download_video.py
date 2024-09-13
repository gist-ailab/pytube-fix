from pytube import YouTube, Playlist
from tqdm import tqdm
import os

p = Playlist('https://www.youtube.com/playlist?list=PLvbUC2Zh5oJtYXow4jawpZJ2xBel6vGhC')
v = YouTube('https://youtu.be/3rLJo7QJXh0')


download_path = './download_path'

### download single video file
# v.streams.filter(only_audio=True).first().download(output_path=download_path, filename=f'{v.title}.mp3')
v.streams.filter(res="720p").first().download(output_path=download_path, filename=f'{v.title}.mp4')
# v.streams.filter().first().download(output_path=download_path, filename=f'{v.title}.mp4')


# '''download video playlist'''
# print(f'Downloading: {p.title}')
# for video in tqdm(p.videos, desc='video download'):
#     # caption = video.captions["en-US"]
#     # caption = video.captions.get_by_language_code('en.j3PyPqV-e1s')
#     # # caption.generate_srt_captions()
#     # caption.download(title=video.title, srt=False, output_path='./download_path')
#
#     # video.streams 안에 객체들이 있는데 이 중 'audio_code'이 있는 것만 소리 나옴
#     stream = video.streams.get_by_itag(22)
#     stream.download(output_path='./download_path')
#     # video.streams.first().download('/download_path')
#     video.streams.filter(res="720p").first().download(output_path='/home/bak/Projects/pytube/download_path', filename=f'{video.title}.mp4')
#     # video.streams.download('/home/bak/Videos')
# # caption = v.captions["en-US"]
# # print(caption.generate_srt_captions())



# '''download audio playlist'''
# print(f'Downloading: {p.title}')
# for i, url in tqdm(enumerate(p.video_urls), desc='audio download'):
#     i = i +1
#     # # caption = video.captions["en-US"]
#     # caption = video.captions.get_by_language_code('en-US')
#     # # caption.generate_srt_captions()
#     # caption.download(title=video.title, srt=False)
#     video = YouTube(url)
#     print('\nDownloading ', f'{format(i, "02")}. {video.title}')
#     if '/' in video.title:
#         video.title = video.title.replace('/', '_')
#     video.streams.filter(only_audio=True).first().download(output_path=download_path, filename=f'{format(i, "02")}. {video.title}.mp3')
#     print(f'{format(i, "02")}. {video.title}.mp3' + ' is downloaded')




# PATH = os.path.abspath('./CS224W')
#
# # print(os.listdir(PATH))
#
# for filename in os.listdir(PATH):
#     # print(filename[-12:-4])
#     print(filename)
#     # if filename[54] == ' ':
#     #     change_name = filename[51:53] + '.' + filename[53:]
#     #     os.rename(PATH + '/' + filename, change_name)
#     # elif filename[53] == ' ':
#     #     change_name = filename[51] + '.' + filename[52:]
#     #     os.rename(PATH + '/' + filename, change_name)
#     change_name = filename[:-12] + filename[-4:]
#     print(change_name)
#     os.rename(PATH + '/' + filename, PATH + '/' + change_name)
