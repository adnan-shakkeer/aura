from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.sales_report, name="sales_report"),
    path("export-csv/", views.sales_report_csv, name="sales_report_csv"),
]
