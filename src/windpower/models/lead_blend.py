"""Six-hour horizon bins for validation-fitted convex prediction weights."""
import numpy as np
from windpower.models.validation_blend import fit_weights


def groups(leads):
    leads=np.asarray(leads)
    if not np.isfinite(leads).all() or ((leads<1)|(leads>24)|(leads!=leads.astype(int))).any():
        raise ValueError('Lead must be integer 1..24')
    return (leads.astype(int)-1)//6


def fit_lead_weights(predictions,actual,leads):
    x,y=np.asarray(predictions,float),np.asarray(actual,float)
    group=groups(leads)
    global_weights=fit_weights(x,y)
    weights={}
    for index in range(4):
        mask=group==index
        if not mask.any():raise ValueError('Missing lead bin')
        weights[index]=fit_weights(x[mask],y[mask])
    return global_weights,weights


def predict_lead_blend(predictions,leads,global_weights,bin_weights,strength):
    if not 0<=strength<=1:raise ValueError('Strength must be 0..1')
    x=np.asarray(predictions,float); group=groups(leads)
    output=np.empty(len(x))
    for index in range(4):
        mask=group==index
        weights=(1-strength)*np.asarray(global_weights)+strength*np.asarray(bin_weights[index])
        output[mask]=x[mask]@weights
    return output
