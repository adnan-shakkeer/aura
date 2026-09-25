from django.shortcuts import render
from django.http import HttpResponse
from django.views import View



class CategoryListView(View):
    def get(self,request):

        return HttpResponse("Category List CBV")

class CategoryCreateView(View):
    def get(self,request):

        return HttpResponse("Category Add CBV")

class CategoryUpdateView(View):
    def post(self,request,category_id):

        return HttpResponse(f"Category Edit CBV for ID {category_id}") 

class CategoryDeleteView(View):
    def post(self,request,category_id):

        return HttpResponse(f"Category Delete CBV for ID {category_id}")

    




