from django.apps import AppConfig


class AbnormalwanderingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "AbnormalWandering"

    # 可选：如果你需要在应用就绪时执行某些操作
    def ready(self):
        # 在这里导入信号处理器或其他初始化代码
        # 但不要导入Admin注册代码
        pass
