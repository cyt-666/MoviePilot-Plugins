# MoviePilot-Plugins
MoviePilot插件市场：https://github.com/cyt-666/MoviePilot-Plugins

## 说明
因为个人追剧app用的是trakt，并且emby和plex可以同步库和播放信息到trakt。
因此参考豆瓣同步插件写了这个trakt同步插件，当前只有一个功能，就是将trakt的watch list添加到MP的订阅里。




## 使用方法

调用Trakt的API需要在trakt的网站上注册一个app，注册位置在个人设置里



注册的时候redirt_url填写：`urn:ietf:wg:oauth:2.0:oob`




会给生成clinet id和client secret


装上插件后在插件的配置里填写clinet id和client secret

点击保存

